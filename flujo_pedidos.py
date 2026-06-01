import json
import re
from negocio_router import cargar_negocios, obtener_negocio

_estados = {}           # numero_cliente -> {codigo, items, estado, direccion, referencia}
_ordenes_pendientes = {}  # numero_cliente -> {codigo, items, total, direccion, referencia}

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
    if item["unidad"] == "libra":
        return f"• {item['texto']} de {item['nombre']} - ${item['precio']:.0f} pesos"
    return f"• {item['texto']}x {item['nombre']} - ${item['precio']:.0f} pesos"


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


def tiene_flujo_activo(numero_cliente):
    return numero_cliente in _estados


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
    activos = datos["negocios"][codigo].setdefault("pedidos_activos", [])
    activos[:] = [p for p in activos if p["numero_cliente"] != numero_cliente]
    activos.append({
        "numero_cliente": numero_cliente,
        "items":          pedido["items"],
        "total":          pedido["total"],
        "direccion":      pedido["direccion"],
        "referencia":     pedido["referencia"],
    })
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
    _guardar(datos)


def _cargar_pedidos_al_inicio():
    try:
        datos = cargar_negocios()
        for codigo, neg in datos["negocios"].items():
            for pedido in neg.get("pedidos_activos", []):
                nc = pedido["numero_cliente"]
                _ordenes_pendientes[nc] = {
                    "codigo":    codigo,
                    "items":     pedido["items"],
                    "total":     pedido["total"],
                    "direccion": pedido["direccion"],
                    "referencia": pedido["referencia"],
                }
                # Restaurar estado para que el cliente pueda cancelar
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
        if s in ("pedido_enviado", "esperando_decision"):
            twilio_send(negocio["numero_negocio"],
                        f"El cliente {numero_cliente} cancelo su pedido.")
        _estados.pop(numero_cliente, None)
        _ordenes_pendientes.pop(numero_cliente, None)
        _eliminar_pedido(codigo, numero_cliente)
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

        for clave, prod, cantidad, texto in disponibles:
            estado["items"].append({
                "clave": clave, "nombre": prod["nombre"],
                "cantidad": cantidad, "texto": texto,
                "unidad": prod["unidad"], "precio": prod["precio"] * cantidad,
            })

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

        _ordenes_pendientes[numero_cliente] = {
            "codigo": codigo, "items": list(items), "total": total,
            "direccion": estado["direccion"], "referencia": estado["referencia"],
        }
        _guardar_pedido(codigo, numero_cliente)
        estado["estado"] = "pedido_enviado"

        # Notificar al negocio
        txt  = f"NUEVO PEDIDO de {numero_cliente}\n\n"
        txt += "\n".join(_fmt(i) for i in items)
        txt += f"\n\nTotal: ${total:.0f} pesos"
        txt += f"\nDireccion: {estado['direccion']}"
        txt += f"\nReferencia: {estado['referencia']}"
        txt += "\n\nSi algo no esta disponible escribe: no hay [producto]"
        twilio_send(negocio["numero_negocio"], txt)

        # Confirmación al cliente — 2 recordatorios en negrita
        r  = f"Pedido enviado a {negocio['nombre']}!\n\n"
        r += "Tu pedido:\n"
        r += "\n".join(_fmt(i) for i in items)
        r += f"\n\nTotal: ${total:.0f} pesos"
        r += f"\nDireccion: {estado['direccion']}"
        r += f"\nReferencia: {estado['referencia']}"
        r += "\n\n*Tu pedido esta PENDIENTE — algo podria no estar disponible.*"
        r += "\n\nPuedes escribir *cancelar* si cambias de opinion antes de que sea procesado."
        r += "\n\n*Recuerda: tu pedido sigue PENDIENTE hasta que el negocio lo confirme.*"
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

    # ── ESPERANDO DECISION (producto no disponible) ──
    if s == "esperando_decision":
        item = estado.get("item_sin_stock", {})
        nombre = item.get("nombre", "ese producto")

        if "continuar" in msg:
            estado["items"] = [i for i in estado["items"] if i["clave"] != item.get("clave")]
            if not estado["items"]:
                _estados.pop(numero_cliente, None)
                _ordenes_pendientes.pop(numero_cliente, None)
                _eliminar_pedido(codigo, numero_cliente)
                return "Tu pedido quedó vacío. Escribe el codigo del negocio cuando quieras hacer un nuevo pedido."
            total = sum(i["precio"] for i in estado["items"])
            _ordenes_pendientes[numero_cliente] = {
                "codigo": codigo, "items": list(estado["items"]), "total": total,
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
            _ordenes_pendientes[numero_cliente] = {
                "codigo": codigo, "items": list(items), "total": total,
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
            for clave, prod, cantidad, texto in disponibles:
                estado["items"].append({
                    "clave": clave, "nombre": prod["nombre"],
                    "cantidad": cantidad, "texto": texto,
                    "unidad": prod["unidad"], "precio": prod["precio"] * cantidad,
                })
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
        # Itera en orden inverso para tomar el pedido más reciente primero
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
        print(f"[DEBUG listo] negocio={codigo_negocio} ordenes_pendientes={list(_ordenes_pendientes.keys())}")
        for cliente, pedido in list(_ordenes_pendientes.items()):
            if pedido["codigo"] == codigo_negocio:
                twilio_send(cliente, "🛵 Tu pedido esta en camino!")
                _eliminar_pedido(codigo_negocio, cliente)
                _ordenes_pendientes.pop(cliente, None)
                _estados.pop(cliente, None)
                return f"Pedido de {cliente} marcado como completado."
        return "No hay pedidos pendientes."

    return None
