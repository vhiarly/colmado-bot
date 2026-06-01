import json
import re
from negocio_router import cargar_negocios, obtener_negocio

_estados = {}           # numero_cliente -> {codigo, items, estado, direccion, referencia}
_ordenes_pendientes = {}  # numero_cliente -> {codigo, turno, items, total, direccion, referencia}
_colas      = {}        # codigo -> [numero_cliente, ...] en orden FIFO
_contadores = {}        # codigo -> int (último turno asignado)

_CONFIRMAR = {"confirmar", "confirma", "si", "sí", "dale", "ok", "okay", "listo", "va", "adelante", "procede"}
_CANCELAR  = {"cancelar", "cancel", "salir", "exit", "bye", "chao", "nada", "olvida", "adios", "adiós"}


def _norm(t):
    t = t.lower().strip()
    for a, b in [("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ü","u"),("ñ","n")]:
        t = t.replace(a, b)
    return t


def _extraer_cantidad(msg, nombre_norm, unidad):
    if unidad == "libra":
        FRACCIONES = [
            ("tres cuartos", 0.75), ("3/4", 0.75),
            ("media libra",  0.5),  ("media", 0.5), ("1/2", 0.5),
            ("un cuarto",    0.25), ("cuarto", 0.25), ("1/4", 0.25),
        ]
        for frase, val in FRACCIONES:
            if frase in msg:
                return val, frase
        m = re.search(r"(\d+(?:\.\d+)?)\s*libra", msg)
        if m:
            v = float(m.group(1))
            return v, f"{m.group(1)} libra{'s' if v != 1 else ''}"
        return 1.0, "1 libra"
    else:
        idx = msg.find(nombre_norm)
        if idx >= 0:
            m = re.search(r"(\d+)\s*$", msg[max(0, idx - 10):idx].strip())
            if m:
                return int(m.group(1)), m.group(1)
            m = re.search(r"^(\d+)", msg[idx + len(nombre_norm):idx + len(nombre_norm) + 10].strip())
            if m:
                return int(m.group(1)), m.group(1)
        return 1, "1"


def _parsear_productos(mensaje, catalogo):
    """Retorna (disponibles, agotados). disponibles = lista de tuplas; agotados = lista de nombres."""
    msg = _norm(mensaje)
    disponibles = []
    agotados = []
    for clave, prod in catalogo.items():
        if not prod.get("activo", True):
            continue
        nombre_norm = _norm(prod["nombre"])
        if nombre_norm not in msg and clave not in msg:
            continue
        if prod.get("cantidad", 1) <= 0:
            agotados.append(prod["nombre"])
            continue
        cantidad, texto = _extraer_cantidad(msg, nombre_norm, prod["unidad"])
        disponibles.append((clave, prod, cantidad, texto))
    return disponibles, agotados


def _fmt(item):
    pref = f" ({item['rebanado_pref']})" if item.get("rebanado_pref") else ""
    if item["unidad"] == "libra":
        return f"• {item['texto']} de {item['nombre']}{pref} - ${item['precio']:.0f} pesos"
    return f"• {item['texto']}x {item['nombre']}{pref} - ${item['precio']:.0f} pesos"


def _menu(negocio):
    lineas = [f"Bienvenido a {negocio['nombre']}!\n\nNuestros productos:\n"]
    for clave, prod in negocio.get("catalogo", {}).items():
        if prod.get("activo", True) and prod.get("cantidad", 1) > 0:
            suf = "/libra" if prod["unidad"] == "libra" else ""
            lineas.append(f"• {prod['nombre']} - ${prod['precio']} pesos{suf}")
    lineas += ["", "Escribe lo que quieres pedir.", "Escribe *cancelar* para salir."]
    return "\n".join(lineas)


def _resumen(items, pie=""):
    total = sum(i["precio"] for i in items)
    lineas = ["Tu orden:\n"] + [_fmt(i) for i in items] + [f"\nTotal: ${total:.0f} pesos"]
    if pie:
        lineas.append(pie)
    return "\n".join(lineas)


def _siguiente_turno(codigo):
    n = _contadores.get(codigo, 0) + 1
    _contadores[codigo] = n
    return n


def _notificar_posiciones(codigo, twilio_send):
    """Envía a cada cliente en espera (índice 1+) su posición actualizada en la cola."""
    cola = _colas.get(codigo, [])
    for i, cliente in enumerate(cola[1:], start=1):
        s = "s" if i > 1 else ""
        twilio_send(cliente, f"Hay {i} pedido{s} antes que el tuyo. Te avisamos cuando sea tu turno.")


def _enviar_pedido_a_negocio(numero_negocio, cliente, pedido, twilio_send, prefijo="NUEVO PEDIDO"):
    turno = pedido.get("turno", "?")
    txt  = f"{prefijo} — Turno #T-{turno} de {cliente}\n\n"
    txt += "\n".join(_fmt(i) for i in pedido.get("items", []))
    txt += f"\n\nTotal: ${pedido.get('total', 0):.0f} pesos"
    txt += f"\nDireccion: {pedido.get('direccion', '')}"
    txt += f"\nReferencia: {pedido.get('referencia', '')}"
    txt += "\n\nSi algo no esta disponible escribe: no hay [producto]"
    twilio_send(numero_negocio, txt)


def tiene_flujo_activo(numero_cliente):
    return numero_cliente in _estados


def limpiar_flujo(numero_cliente):
    estado = _estados.get(numero_cliente, {})
    codigo = estado.get("codigo")
    if codigo:
        cola = _colas.get(codigo, [])
        if numero_cliente in cola:
            cola.remove(numero_cliente)
        _eliminar_pedido(codigo, numero_cliente)
    _estados.pop(numero_cliente, None)
    _ordenes_pendientes.pop(numero_cliente, None)


def es_numero_negocio(numero):
    """Retorna el codigo si el numero pertenece a un negocio registrado, o None."""
    datos = cargar_negocios()
    for cod, neg in datos["negocios"].items():
        print(f"[DEBUG es_numero_negocio] comparando {numero} vs {neg['numero_negocio']} ({cod})")
        if neg["numero_negocio"] == numero:
            return cod
    return None


# ── Persistencia de pedidos ───────────────────────────────────────────────────

def _guardar(datos):
    import negocio_router
    with open("negocios.json", "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    negocio_router._negocios_cache = datos


def _guardar_pedido(codigo, numero_cliente):
    datos = cargar_negocios()
    pedido = _ordenes_pendientes[numero_cliente]
    neg = datos["negocios"][codigo]
    activos = neg.setdefault("pedidos_activos", [])
    activos[:] = [p for p in activos if p["numero_cliente"] != numero_cliente]
    activos.append({
        "numero_cliente": numero_cliente,
        "turno":          pedido.get("turno"),
        "items":          pedido["items"],
        "total":          pedido["total"],
        "direccion":      pedido["direccion"],
        "referencia":     pedido["referencia"],
    })
    neg["contador_turnos"] = _contadores.get(codigo, 0)
    neg["cola_turnos"] = list(_colas.get(codigo, []))
    _guardar(datos)


def _eliminar_pedido(codigo, numero_cliente):
    datos = cargar_negocios()
    neg = datos["negocios"].get(codigo)
    if not neg:
        return
    neg["pedidos_activos"] = [
        p for p in neg.get("pedidos_activos", [])
        if p["numero_cliente"] != numero_cliente
    ]
    neg["cola_turnos"] = list(_colas.get(codigo, []))
    _guardar(datos)


def _cargar_pedidos_al_inicio():
    try:
        datos = cargar_negocios()
        for codigo, neg in datos["negocios"].items():
            _colas[codigo]      = neg.get("cola_turnos", [])
            _contadores[codigo] = neg.get("contador_turnos", 0)
            for pedido in neg.get("pedidos_activos", []):
                nc = pedido["numero_cliente"]
                _ordenes_pendientes[nc] = {
                    "codigo":    codigo,
                    "turno":     pedido.get("turno"),
                    "items":     pedido["items"],
                    "total":     pedido["total"],
                    "direccion": pedido["direccion"],
                    "referencia": pedido["referencia"],
                }
                _estados[nc] = {
                    "codigo":    codigo,
                    "items":     pedido["items"],
                    "estado":    "pedido_enviado",
                    "direccion": pedido["direccion"],
                    "referencia": pedido["referencia"],
                }
        print(f"[INICIO] Pedidos cargados desde disco: {list(_ordenes_pendientes.keys())}")
    except Exception as e:
        print(f"[INICIO] Error cargando pedidos: {e}")


_cargar_pedidos_al_inicio()


def manejar_pedido(numero_cliente, codigo, mensaje, twilio_send):
    """
    Maneja el flujo completo de pedidos de un negocio.
    Retorna str (respuesta al cliente) o None.
    twilio_send(to, body) para mensajes proactivos.
    """
    msg = _norm(mensaje)

    if numero_cliente in _estados:
        codigo = _estados[numero_cliente]["codigo"]
    elif not codigo:
        return None

    negocio = obtener_negocio(codigo)
    if not negocio:
        return "Negocio no encontrado."

    if numero_cliente not in _estados:
        _estados[numero_cliente] = {
            "codigo": codigo, "items": [], "estado": "pidiendo",
            "direccion": "", "referencia": "",
        }

    estado = _estados[numero_cliente]
    s = estado["estado"]

    # Cancelar desde cualquier estado
    if any(re.search(r"\b" + p + r"\b", msg) for p in _CANCELAR):
        era_primero = False
        if s in ("pedido_enviado", "esperando_decision"):
            cola = _colas.get(codigo, [])
            era_primero = bool(cola) and cola[0] == numero_cliente
            if numero_cliente in cola:
                cola.remove(numero_cliente)
            twilio_send(negocio["numero_negocio"],
                        f"El cliente {numero_cliente} cancelo su pedido.")
        _estados.pop(numero_cliente, None)
        _ordenes_pendientes.pop(numero_cliente, None)
        _eliminar_pedido(codigo, numero_cliente)
        if era_primero:
            cola_actual = _colas.get(codigo, [])
            if cola_actual:
                siguiente = cola_actual[0]
                _enviar_pedido_a_negocio(negocio["numero_negocio"], siguiente,
                                         _ordenes_pendientes[siguiente], twilio_send,
                                         prefijo="SIGUIENTE PEDIDO")
                twilio_send(siguiente, "Tu pedido está siendo preparado, sale en unos minutos!")
                _notificar_posiciones(codigo, twilio_send)
        return "Orden cancelada. Escribe el codigo del negocio cuando quieras pedir de nuevo."

    # ── PIDIENDO ──
    if s == "pidiendo":
        if not msg or any(p in msg for p in ["hola", "buenas", "menu", "menú", "que tienen"]):
            return _menu(negocio)

        if any(re.search(r"\b" + p + r"\b", msg) for p in _CONFIRMAR):
            if not estado["items"]:
                return "No tienes productos en tu orden. Escribe *menú* para ver lo que tenemos."
            estado["estado"] = "esperando_confirmacion"
            return _resumen(estado["items"],
                            "\nEscribe *sí* para confirmar o *cancelar* para salir.")

        disponibles, agotados = _parsear_productos(mensaje, negocio.get("catalogo", {}))

        if not disponibles:
            if agotados:
                return "Ese producto está agotado ahorita. Escribe *menú* para ver lo que tenemos."
            return "No encontre ese producto. Escribe *menú* para ver lo que tenemos."

        cola_rebanado = []
        for clave, prod, cantidad, texto in disponibles:
            item = {
                "clave": clave, "nombre": prod["nombre"],
                "cantidad": cantidad, "texto": texto,
                "unidad": prod["unidad"], "precio": prod["precio"] * cantidad,
            }
            if prod.get("rebanado"):
                cola_rebanado.append(item)
            else:
                estado["items"].append(item)

        if cola_rebanado:
            primero = cola_rebanado.pop(0)
            estado["item_pendiente_rebanado"] = primero
            estado["cola_rebanado"] = cola_rebanado
            estado["rebanado_origen"] = "pidiendo"
            estado["estado"] = "esperando_rebanado"
            return (f"¿Cómo quieres el {primero['nombre']}?\n\n"
                    "• Escribe *rebanado*\n"
                    "• Escribe *en pieza*")

        respuesta = _resumen(estado["items"], "\nEscribe mas productos o *confirmar* para pedir.")
        if agotados:
            respuesta += f"\n\n(Nota: {', '.join(agotados)} está agotado y no se agregó a tu orden.)"
        return respuesta

    # ── ESPERANDO CONFIRMACION ──
    if s == "esperando_confirmacion":
        if any(re.search(r"\b" + p + r"\b", msg) for p in _CONFIRMAR):
            estado["estado"] = "esperando_direccion"
            return ("A que direccion te enviamos?\n\n"
                    "Ejemplo: Calle Duarte 45, Los Jardines, Santo Domingo")
        return _resumen(estado["items"],
                        "\nEscribe *sí* para confirmar o *cancelar* para salir.")

    # ── ESPERANDO DIRECCIÓN ──
    if s == "esperando_direccion":
        estado["direccion"] = mensaje
        estado["estado"] = "esperando_referencia"
        return ("Alguna referencia para encontrarte mas facil?\n\n"
                "Ejemplo: Al lado de la farmacia, Casa azul\n\n"
                "Si no tienes, escribe *ninguna*.")

    # ── ESPERANDO REFERENCIA ──
    if s == "esperando_referencia":
        estado["referencia"] = mensaje if msg != "ninguna" else "Sin referencia"
        items = estado["items"]
        total = sum(i["precio"] for i in items)

        turno = _siguiente_turno(codigo)
        cola  = _colas.setdefault(codigo, [])
        cola.append(numero_cliente)
        posicion = len(cola)

        _ordenes_pendientes[numero_cliente] = {
            "codigo": codigo, "items": list(items), "total": total,
            "turno": turno,
            "direccion": estado["direccion"], "referencia": estado["referencia"],
        }
        _guardar_pedido(codigo, numero_cliente)
        estado["estado"] = "pedido_enviado"

        if posicion == 1:
            _enviar_pedido_a_negocio(negocio["numero_negocio"], numero_cliente,
                                     _ordenes_pendientes[numero_cliente], twilio_send)

        r  = f"Pedido enviado a {negocio['nombre']}! Turno *#T-{turno}*\n\n"
        r += "Tu pedido:\n"
        r += "\n".join(_fmt(i) for i in items)
        r += f"\n\nTotal: ${total:.0f} pesos"
        r += f"\nDireccion: {estado['direccion']}"
        r += f"\nReferencia: {estado['referencia']}"
        if posicion == 1:
            r += "\n\n*Tu pedido está siendo preparado.*"
        else:
            s_plural = "s" if posicion - 1 > 1 else ""
            r += f"\n\nHay {posicion - 1} pedido{s_plural} antes que el tuyo. Te avisamos cuando sea tu turno."
        r += "\n\nPuedes escribir *cancelar* si cambias de opinion antes de que sea procesado."
        return r

    # ── PEDIDO ENVIADO ──
    if s == "pedido_enviado":
        if "ajustar" in msg:
            estado["estado"] = "ajustando"
            return (_resumen(estado["items"]) +
                    "\n\nQue quieres cambiar?\n\n"
                    "• *quitar* [producto] para eliminarlo\n"
                    "• escribe un producto para agregarlo\n"
                    "• *listo* para confirmar los cambios")
        return ("*Tu pedido esta pendiente.*\n\n"
                "Escribe *ajustar* para modificarlo o *cancelar* para cancelarlo.")

    # ── ESPERANDO REBANADO ──
    if s == "esperando_rebanado":
        item = estado["item_pendiente_rebanado"]
        nombre = item["nombre"]

        if any(p in msg for p in ["rebanado", "rebana", "rebanada"]):
            item["rebanado_pref"] = "rebanado"
        elif any(p in msg for p in ["pieza", "entero", "entera", "sin rebanar"]):
            item["rebanado_pref"] = "en pieza"
        else:
            return (f"¿Cómo quieres el {nombre}?\n\n"
                    "• Escribe *rebanado*\n"
                    "• Escribe *en pieza*\n"
                    "• Escribe *cancelar* para cancelar el pedido")

        estado["items"].append(item)
        cola = estado.get("cola_rebanado", [])

        if cola:
            siguiente = cola.pop(0)
            estado["item_pendiente_rebanado"] = siguiente
            estado["cola_rebanado"] = cola
            return (f"¿Cómo quieres el {siguiente['nombre']}?\n\n"
                    "• Escribe *rebanado*\n"
                    "• Escribe *en pieza*")

        estado.pop("item_pendiente_rebanado", None)
        estado.pop("cola_rebanado", None)
        origen = estado.pop("rebanado_origen", "pidiendo")
        estado["estado"] = origen

        if origen == "ajustando":
            return _resumen(estado["items"], "\nSigue ajustando o escribe *listo* para confirmar.")
        return _resumen(estado["items"], "\nEscribe mas productos o *confirmar* para pedir.")

    # ── ESPERANDO DECISION (producto no disponible) ──
    if s == "esperando_decision":
        item = estado.get("item_sin_stock", {})
        nombre = item.get("nombre", "ese producto")

        if "continuar" in msg:
            estado["items"] = [i for i in estado["items"] if i["clave"] != item.get("clave")]
            if not estado["items"]:
                cola = _colas.get(codigo, [])
                era_primero = bool(cola) and cola[0] == numero_cliente
                if numero_cliente in cola:
                    cola.remove(numero_cliente)
                _estados.pop(numero_cliente, None)
                _ordenes_pendientes.pop(numero_cliente, None)
                _eliminar_pedido(codigo, numero_cliente)
                if era_primero:
                    cola_actual = _colas.get(codigo, [])
                    if cola_actual:
                        siguiente = cola_actual[0]
                        _enviar_pedido_a_negocio(negocio["numero_negocio"], siguiente,
                                                 _ordenes_pendientes[siguiente], twilio_send,
                                                 prefijo="SIGUIENTE PEDIDO")
                        twilio_send(siguiente, "Tu pedido está siendo preparado, sale en unos minutos!")
                        _notificar_posiciones(codigo, twilio_send)
                return "Tu pedido quedó vacío. Escribe el codigo del negocio cuando quieras hacer un nuevo pedido."

            turno_existente = _ordenes_pendientes.get(numero_cliente, {}).get("turno")
            total = sum(i["precio"] for i in estado["items"])
            _ordenes_pendientes[numero_cliente] = {
                "codigo": codigo, "items": list(estado["items"]), "total": total,
                "turno": turno_existente,
                "direccion": estado["direccion"], "referencia": estado["referencia"],
            }
            _guardar_pedido(codigo, numero_cliente)
            estado["estado"] = "pedido_enviado"
            estado.pop("item_sin_stock", None)
            txt  = f"PEDIDO ACTUALIZADO de {numero_cliente} — se eliminó {nombre}\n\n"
            txt += "\n".join(_fmt(i) for i in estado["items"])
            txt += f"\n\nTotal: ${total:.0f} pesos"
            twilio_send(negocio["numero_negocio"], txt)
            return _resumen(estado["items"], "\n\nPedido actualizado. Tu orden sigue en camino.")

        return (f"¿Qué prefieres?\n\n"
                f"• Escribe *continuar* para seguir sin {nombre}\n"
                f"• Escribe *cancelar* para cancelar el pedido")

    # ── AJUSTANDO ──
    if s == "ajustando":
        if re.search(r"\blisto\b", msg):
            items = estado["items"]
            total = sum(i["precio"] for i in items)
            turno_existente = _ordenes_pendientes.get(numero_cliente, {}).get("turno")
            _ordenes_pendientes[numero_cliente] = {
                "codigo": codigo, "items": list(items), "total": total,
                "turno": turno_existente,
                "direccion": estado["direccion"], "referencia": estado["referencia"],
            }
            _guardar_pedido(codigo, numero_cliente)
            estado["estado"] = "pedido_enviado"

            txt  = f"PEDIDO AJUSTADO de {numero_cliente}\n\n"
            txt += "\n".join(_fmt(i) for i in items)
            txt += f"\n\nTotal: ${total:.0f} pesos"
            txt += f"\nDireccion: {estado['direccion']}"
            txt += f"\nReferencia: {estado['referencia']}"
            twilio_send(negocio["numero_negocio"], txt)

            return _resumen(items, "\n\nPedido actualizado y reenviado al negocio.")

        m = re.match(r"quitar\s+(.+)", msg)
        if m:
            buscado = m.group(1).strip()
            antes = len(estado["items"])
            estado["items"] = [i for i in estado["items"] if buscado not in _norm(i["nombre"])]
            if len(estado["items"]) == antes:
                return f"No encontre '{buscado}' en tu pedido."
            if not estado["items"]:
                return "Eliminaste todos los productos. Agrega algo o escribe *cancelar*."
            return _resumen(estado["items"], "\nSigue ajustando o escribe *listo* para confirmar.")

        disponibles, agotados = _parsear_productos(mensaje, negocio.get("catalogo", {}))
        if disponibles:
            cola_rebanado = []
            for clave, prod, cantidad, texto in disponibles:
                item = {
                    "clave": clave, "nombre": prod["nombre"],
                    "cantidad": cantidad, "texto": texto,
                    "unidad": prod["unidad"], "precio": prod["precio"] * cantidad,
                }
                if prod.get("rebanado"):
                    cola_rebanado.append(item)
                else:
                    estado["items"].append(item)

            if cola_rebanado:
                primero = cola_rebanado.pop(0)
                estado["item_pendiente_rebanado"] = primero
                estado["cola_rebanado"] = cola_rebanado
                estado["rebanado_origen"] = "ajustando"
                estado["estado"] = "esperando_rebanado"
                return (f"¿Cómo quieres el {primero['nombre']}?\n\n"
                        "• Escribe *rebanado*\n"
                        "• Escribe *en pieza*")

            return _resumen(estado["items"], "\nSigue ajustando o escribe *listo* para confirmar.")

        if agotados:
            return "Ese producto está agotado ahorita. Escribe *menú* para ver lo que tenemos."

        return "No entendi. Escribe *quitar* [producto], agrega un producto, o *listo* para confirmar."

    return None


def manejar_negocio(numero_negocio, codigo_negocio, mensaje, twilio_send):
    """
    Maneja mensajes del negocio al bot.
    Retorna str (respuesta al negocio) o None.
    """
    msg = _norm(mensaje)

    # "no hay [producto]" → notificar al cliente
    m = re.match(r"no\s+hay\s+(.+)", msg)
    if m:
        buscado = m.group(1).strip()
        print(f"[DEBUG no hay] negocio={codigo_negocio} buscado='{buscado}' ordenes_pendientes={list(_ordenes_pendientes.keys())}")
        for cliente, pedido in reversed(list(_ordenes_pendientes.items())):
            if pedido["codigo"] != codigo_negocio:
                continue
            for item in pedido["items"]:
                if buscado in _norm(item["nombre"]):
                    if cliente in _estados:
                        _estados[cliente]["estado"] = "esperando_decision"
                        _estados[cliente]["item_sin_stock"] = item
                    twilio_send(
                        cliente,
                        f"Lo sentimos, *{item['nombre']}* no está disponible. ¿Qué prefieres?\n\n"
                        "• Escribe *continuar* para seguir sin ese producto\n"
                        "• Escribe *cancelar* para cancelar el pedido"
                    )
                    return f"Cliente notificado sobre {item['nombre']}."
        return "No encontre pedidos pendientes con ese producto."

    # "listo" → pedido despachado
    if re.search(r"\blisto\b", msg):
        print(f"[DEBUG listo] negocio={codigo_negocio} cola={_colas.get(codigo_negocio, [])}")
        cola = _colas.get(codigo_negocio, [])
        if not cola:
            return "No hay pedidos pendientes."

        cliente_actual = cola[0]
        if _estados.get(cliente_actual, {}).get("estado") == "esperando_decision":
            return "El pedido actual tiene un producto pendiente de decisión del cliente. Espera su respuesta."

        turno_actual = _ordenes_pendientes.get(cliente_actual, {}).get("turno", "?")
        twilio_send(cliente_actual, "🛵 Tu pedido está en camino!")
        cola.pop(0)
        _eliminar_pedido(codigo_negocio, cliente_actual)
        _ordenes_pendientes.pop(cliente_actual, None)
        _estados.pop(cliente_actual, None)

        if not cola:
            return "✅ Listo! No hay más pedidos por ahora."

        siguiente = cola[0]
        _enviar_pedido_a_negocio(numero_negocio, siguiente,
                                 _ordenes_pendientes[siguiente], twilio_send,
                                 prefijo="SIGUIENTE PEDIDO")
        twilio_send(siguiente, "Tu pedido está siendo preparado, sale en unos minutos!")
        _notificar_posiciones(codigo_negocio, twilio_send)

        turno_sig = _ordenes_pendientes[siguiente].get("turno", "?")
        return f"Turno #T-{turno_actual} despachado. Enviando turno #T-{turno_sig} al siguiente."

    return None
