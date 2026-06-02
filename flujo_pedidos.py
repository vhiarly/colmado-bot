import re
from datetime import date
from psycopg2.extras import Json
from db import execute
from negocio_router import obtener_negocio

_CONFIRMAR = {"confirmar", "confirma", "si", "sí", "dale", "ok", "okay", "listo", "va", "adelante", "procede"}
_CANCELAR  = {"cancelar", "cancel", "salir", "exit", "bye", "chao", "nada", "olvida", "adios", "adiós",
              "nop", "no quiero", "paso"}

_CONSULTA_PATTERNS = [r"\bhay\b", r"\bahi\b", r"\bay\b", r"\btienen\b", r"\btienes\b"]

_ALIAS = {
    "presidente": "presidente", "presidentes": "presidente",
    "presiden": "presidente",   "presi": "presidente",
    "heineken": "heineken",     "heini": "heineken", "heines": "heineken",
    "agua": "agua",             "agüita": "agua",    "aguita": "agua",
    "refresco": "refresco",     "refrescos": "refresco",
    "coca": "refresco",         "cocacola": "refresco", "coca cola": "refresco",
    "jugo": "jugo",             "jugos": "jugo",     "tampico": "jugo",
    "salami": "salami",         "salamis": "salami", "salame": "salami",
    "queso bola": "queso bola", "queso de bola": "queso bola",
    "queso amarillo": "queso amarillo", "queso": "queso amarillo",
    "jamon": "jamon pierna",    "jamón": "jamon pierna",
    "jamon pierna": "jamon pierna",     "jamon pavo": "jamon pavo",
    "jamón pavo": "jamon pavo",
    "mortadela": "mortadela",   "mortadelas": "mortadela", "mortade": "mortadela",
    "longaniza": "longaniza",   "longanizas": "longaniza", "longani": "longaniza",
    "arroz": "arroz",
    "habichuelas": "habichuelas", "habichuela": "habichuelas",
    "frijoles": "habichuelas",    "frijol": "habichuelas",
    "azucar": "azucar",           "azúcar": "azucar",
    "cafe": "cafe",    "café": "cafe",    "sandino": "cafe",
    "cafe granel": "cafe granel", "café granel": "cafe granel",
    "aceite": "aceite",           "iberia": "aceite",
    "huevo": "huevo",             "huevos": "huevo",
    "pan": "pan",      "panes": "pan",    "pan de agua": "pan",
    "platano": "platano",  "plátano": "platano",
    "platanos": "platano", "plátanos": "platano", "guineo": "platano",
    "leche": "leche",      "lechita": "leche",
    "mantequilla": "mantequilla", "mante": "mantequilla",
    "cigarrillo": "cigarrillo",   "cigarrillos": "cigarrillo",
    "cigarro": "cigarrillo",      "cigarros": "cigarrillo", "fuma": "cigarrillo",
    "maiz": "maiz",    "maíz": "maiz",
    "cebolla": "cebolla", "cebollas": "cebolla",
    "papa": "papa",    "papas": "papa",   "patata": "papa",
}

_PALABRAS_IGNORAR = {
    "dame", "quiero", "necesito", "ponme", "mandame", "mándame",
    "tráeme", "traeme", "deme", "déme", "una", "uno", "un",
    "por favor", "porfavor", "fa", "xfavor",
    "de", "unidad", "unidades",
    "dos", "tres", "cuatro", "cinco", "seis",
    "siete", "ocho", "nueve", "diez",
    "un cuarto", "tres cuartos",
    "y", "con", "mas", "más", "también", "tambien",
}


def _norm(t):
    t = t.lower().strip()
    for a, b in [("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ü","u"),("ñ","n")]:
        t = t.replace(a, b)
    return t


# ── Helpers de estado de conversación ────────────────────────────────────────

def _get_estado(numero_cliente):
    return execute(
        "SELECT * FROM conversaciones_pedidos WHERE numero_cliente = %s",
        (numero_cliente,), fetch="one"
    )

def _set_estado(numero_cliente, data):
    execute("""
        INSERT INTO conversaciones_pedidos
            (numero_cliente, codigo, estado, items, direccion, referencia,
             item_pendiente_rebanado, cola_rebanado, rebanado_origen,
             item_sin_stock, timeout_en)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() + INTERVAL '5 minutes')
        ON CONFLICT (numero_cliente) DO UPDATE SET
            codigo                  = EXCLUDED.codigo,
            estado                  = EXCLUDED.estado,
            items                   = EXCLUDED.items,
            direccion               = EXCLUDED.direccion,
            referencia              = EXCLUDED.referencia,
            item_pendiente_rebanado = EXCLUDED.item_pendiente_rebanado,
            cola_rebanado           = EXCLUDED.cola_rebanado,
            rebanado_origen         = EXCLUDED.rebanado_origen,
            item_sin_stock          = EXCLUDED.item_sin_stock,
            timeout_en              = EXCLUDED.timeout_en,
            actualizado_en          = NOW()
    """, (
        numero_cliente,
        data["codigo"],
        data["estado"],
        Json(data.get("items", [])),
        data.get("direccion", ""),
        data.get("referencia", ""),
        Json(data["item_pendiente_rebanado"]) if data.get("item_pendiente_rebanado") is not None else None,
        Json(data["cola_rebanado"])           if data.get("cola_rebanado")           is not None else None,
        data.get("rebanado_origen"),
        Json(data["item_sin_stock"])          if data.get("item_sin_stock")          is not None else None,
    ))

def _del_estado(numero_cliente):
    execute("DELETE FROM conversaciones_pedidos WHERE numero_cliente = %s", (numero_cliente,))


# ── Helpers de pedidos y cola ─────────────────────────────────────────────────

def _get_pedido(numero_cliente):
    return execute(
        "SELECT * FROM pedidos WHERE numero_cliente = %s AND estado = 'pendiente'",
        (numero_cliente,), fetch="one"
    )

def _get_cola(codigo):
    rows = execute(
        "SELECT numero_cliente FROM pedidos WHERE codigo = %s AND estado = 'pendiente' ORDER BY creado_en ASC",
        (codigo,), fetch="all"
    )
    return [r["numero_cliente"] for r in rows] if rows else []

def _siguiente_turno(codigo):
    hoy = date.today()
    row = execute("""
        INSERT INTO contadores_turnos (codigo, fecha, contador) VALUES (%s, %s, 1)
        ON CONFLICT (codigo) DO UPDATE SET
            contador = CASE
                WHEN contadores_turnos.fecha = EXCLUDED.fecha
                THEN contadores_turnos.contador + 1
                ELSE 1
            END,
            fecha = EXCLUDED.fecha
        RETURNING contador
    """, (codigo, hoy), fetch="one")
    return row["contador"]

def _guardar_pedido(numero_cliente, codigo, items, total, turno, direccion, referencia):
    execute("""
        INSERT INTO pedidos (numero_cliente, codigo, turno, items, total, direccion, referencia, estado)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pendiente')
        ON CONFLICT DO NOTHING
    """, (numero_cliente, codigo, turno, Json(items), total, direccion, referencia))

def _eliminar_pedido(numero_cliente):
    execute("DELETE FROM pedidos WHERE numero_cliente = %s AND estado = 'pendiente'", (numero_cliente,))


# ── Clasificación numérica y detección de intent ─────────────────────────────

def _clasificar_numero_libra(n):
    """
    Clasifica un número sin unidad explícita en el contexto de un producto por libra.
    1-5 → cantidad en libras; múltiplos de 5 ≥ 10 → monto en pesos DOP; resto → ambiguo.
    """
    if n != int(n):          # decimal → siempre libras
        return "libra"
    n = int(n)
    if 1 <= n <= 5:
        return "libra"
    if n >= 10 and n % 5 == 0:
        return "pesos"
    return "ambiguo"

def _es_consulta(msg):
    return any(re.search(p, msg) for p in _CONSULTA_PATTERNS)

def _catalogo_activo(catalogo):
    return [(k, v) for k, v in catalogo.items()
            if v.get("activo", True) and v.get("cantidad", 1) > 0]

def _seleccion_numerica(msg, catalogo):
    """
    Si msg es un número entero positivo, lo mapea al producto en esa posición del catálogo activo.
    Retorna (clave, prod) o (None, None).
    """
    if not msg.strip().isdigit():
        return None, None
    idx = int(msg.strip()) - 1
    activos = _catalogo_activo(catalogo)
    if 0 <= idx < len(activos):
        return activos[idx]
    return None, None

def _detectar_aclaracion_necesaria(mensaje, catalogo):
    """
    Pre-check para productos por libra con número sin unidad explícita.
    Retorna None si no aplica; o un dict con 'tipo' ('pesos' | 'ambiguo') y datos del producto.
    """
    msg = _norm(mensaje)
    tiene_keyword_pesos = bool(re.search(r'\bpeso[s]?\b|\bdop\b|rd\$', msg))
    if re.search(r'\blibra[s]?\b', msg):   # unidad explícita → _extraer_cantidad lo maneja
        return None

    for clave, prod in _catalogo_activo(catalogo):
        if prod["unidad"] != "libra":
            continue
        nombre_norm = _norm(prod["nombre"])
        if nombre_norm not in msg and clave not in msg:
            continue
        m = re.search(r'\b(\d+(?:\.\d+)?)\b', msg)
        if not m:
            continue
        # skip si el número va seguido de "libra"
        if re.search(r'^\d+(?:\.\d+)?\s*libra', msg[m.start():]):
            continue
        n = float(m.group(1))
        if tiene_keyword_pesos:
            tipo = "pesos"
        else:
            tipo = _clasificar_numero_libra(n)
        if tipo == "libra":
            return None   # _extraer_cantidad lo resuelve normalmente
        return {
            "tipo": tipo, "numero": n,
            "clave": clave, "nombre": prod["nombre"],
            "precio": prod["precio"], "rebanado": prod.get("rebanado", False),
        }
    return None


# ── Utilidades de formato ─────────────────────────────────────────────────────

def _strip_stopwords(msg):
    for w in sorted(_PALABRAS_IGNORAR, key=len, reverse=True):
        msg = re.sub(r'\b' + re.escape(w) + r'\b', ' ', msg)
    return re.sub(r'\s+', ' ', msg).strip()


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
        # Número plano: solo usar si clasificado como libra (1-5 o decimal)
        m = re.search(r'\b(\d+(?:\.\d+)?)\b', msg)
        if m:
            n = float(m.group(1))
            if _clasificar_numero_libra(n) == "libra":
                label = int(n) if n == int(n) else n
                return n, f"{label} libra{'s' if n != 1 else ''}"
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
    msg = _norm(mensaje)
    msg_limpio = _strip_stopwords(msg)
    disponibles = []
    agotados = []
    encontrados = set()

    # Pasada 1: coincidencia directa por nombre o clave
    for clave, prod in catalogo.items():
        if not prod.get("activo", True):
            continue
        nombre_norm = _norm(prod["nombre"])
        if nombre_norm not in msg and clave not in msg:
            continue
        encontrados.add(clave)
        if prod.get("cantidad", 1) <= 0:
            agotados.append(prod["nombre"])
            continue
        cantidad, texto = _extraer_cantidad(msg, nombre_norm, prod["unidad"])
        disponibles.append((clave, prod, cantidad, texto))

    # Pasada 2: alias dominicanos
    for alias, clave_canonical in _ALIAS.items():
        if alias not in msg_limpio:
            continue
        if clave_canonical in encontrados:
            continue
        prod = catalogo.get(clave_canonical)
        if not prod or not prod.get("activo", True):
            continue
        encontrados.add(clave_canonical)
        if prod.get("cantidad", 1) <= 0:
            agotados.append(prod["nombre"])
            continue
        nombre_norm = _norm(prod["nombre"])
        cantidad, texto = _extraer_cantidad(msg_limpio, nombre_norm, prod["unidad"])
        disponibles.append((clave_canonical, prod, cantidad, texto))

    return disponibles, agotados


def _fmt(item):
    pref = f" ({item['rebanado_pref']})" if item.get("rebanado_pref") else ""
    if item["unidad"] == "libra":
        return f"• {item['texto']} de {item['nombre']}{pref} - ${item['precio']:.0f} pesos"
    return f"• {item['texto']}x {item['nombre']}{pref} - ${item['precio']:.0f} pesos"


def _menu(negocio):
    lineas = [f"Bienvenido a {negocio['nombre']}!\n\nNuestros productos:\n"]
    for idx, (clave, prod) in enumerate(_catalogo_activo(negocio.get("catalogo", {})), 1):
        suf = "/libra" if prod["unidad"] == "libra" else ""
        lineas.append(f"{idx}. {prod['nombre']} - ${prod['precio']} pesos{suf}")
    lineas += ["", "Escribe el *número* del producto o su nombre.", "Escribe *cancelar* para salir."]
    return "\n".join(lineas)


def _resumen(items, pie=""):
    total = sum(i["precio"] for i in items)
    lineas = ["Tu orden:\n"] + [_fmt(i) for i in items] + [f"\nTotal: ${total:.0f} pesos"]
    if pie:
        lineas.append(pie)
    return "\n".join(lineas)


# ── Notificaciones ────────────────────────────────────────────────────────────

def _notificar_posiciones(codigo, twilio_send):
    cola = _get_cola(codigo)
    for i, cliente in enumerate(cola[1:], start=1):
        s = "s" if i > 1 else ""
        twilio_send(cliente, f"Avanzaste! Hay {i} puesto{s} antes que el tuyo.")


def _enviar_pedido_a_negocio(numero_negocio, numero_cliente, pedido, twilio_send, prefijo="NUEVO PEDIDO"):
    puesto = pedido.get("turno", "?")
    txt  = f"{prefijo} — Puesto #P-{puesto} de {numero_cliente}\n\n"
    txt += "\n".join(_fmt(i) for i in pedido.get("items", []))
    txt += f"\n\nTotal: ${pedido.get('total', 0):.0f} pesos"
    txt += f"\nDireccion: {pedido.get('direccion', '')}"
    txt += f"\nReferencia: {pedido.get('referencia', '')}"
    txt += "\n\nSi algo no esta disponible escribe: no hay [producto]"
    twilio_send(numero_negocio, txt)


# ── Helper de cancelación con manejo de cola ──────────────────────────────────

def _ejecutar_cancelacion(numero_cliente, codigo, negocio, twilio_send, notificar_negocio=False):
    cola = _get_cola(codigo)
    era_primero = bool(cola) and cola[0] == numero_cliente
    if notificar_negocio:
        twilio_send(negocio["numero_negocio"], f"El cliente {numero_cliente} cancelo su pedido.")
    _eliminar_pedido(numero_cliente)
    _del_estado(numero_cliente)
    if era_primero:
        cola_actual = _get_cola(codigo)
        if cola_actual:
            siguiente = cola_actual[0]
            pedido_sig = _get_pedido(siguiente)
            _enviar_pedido_a_negocio(negocio["numero_negocio"], siguiente,
                                     pedido_sig, twilio_send, prefijo="SIGUIENTE PEDIDO")
            twilio_send(siguiente, "Tu pedido está siendo preparado, sale en unos minutos!")
            _notificar_posiciones(codigo, twilio_send)
    return "Orden cancelada. Escribe el codigo del negocio cuando quieras pedir de nuevo."


# ── API pública ───────────────────────────────────────────────────────────────

def tiene_flujo_activo(numero_cliente):
    return execute(
        "SELECT 1 FROM conversaciones_pedidos WHERE numero_cliente = %s",
        (numero_cliente,), fetch="one"
    ) is not None


def limpiar_flujo(numero_cliente):
    _eliminar_pedido(numero_cliente)
    _del_estado(numero_cliente)


def manejar_pedido(numero_cliente, codigo, mensaje, twilio_send):
    msg = _norm(mensaje)

    estado = _get_estado(numero_cliente)
    if estado:
        codigo = estado["codigo"]
    elif not codigo:
        return None

    negocio = obtener_negocio(codigo)
    if not negocio:
        return "Negocio no encontrado."

    if not estado:
        estado = {
            "numero_cliente": numero_cliente,
            "codigo": codigo, "items": [], "estado": "pidiendo",
            "direccion": "", "referencia": "",
            "item_pendiente_rebanado": None, "cola_rebanado": None,
            "rebanado_origen": None, "item_sin_stock": None,
        }
        _set_estado(numero_cliente, estado)

    s = estado["estado"]
    items = estado.get("items") or []

    # Cancelar desde cualquier estado
    if any(re.search(r"\b" + p + r"\b", msg) for p in _CANCELAR):
        notificar = s in ("pedido_enviado", "esperando_decision")
        return _ejecutar_cancelacion(numero_cliente, codigo, negocio, twilio_send,
                                     notificar_negocio=notificar)

    # ── PIDIENDO ──
    if s == "pidiendo":
        if not msg or any(p in msg for p in ["hola", "buenas", "menu", "menú", "que tienen", "que hay"]):
            return _menu(negocio)

        if any(re.search(r"\b" + p + r"\b", msg) for p in _CONFIRMAR):
            if not items:
                return "No tienes productos en tu orden. Escribe *menú* para ver lo que tenemos."
            estado["estado"] = "esperando_confirmacion"
            _set_estado(numero_cliente, estado)
            return _resumen(items, "\n1. Confirmar pedido\n2. Cancelar")

        # Selección numérica del menú
        clave_sel, prod_sel = _seleccion_numerica(msg, negocio.get("catalogo", {}))
        if clave_sel:
            if prod_sel["unidad"] == "libra":
                p = prod_sel["precio"]
                estado["item_pendiente_rebanado"] = {
                    "clave": clave_sel, "nombre": prod_sel["nombre"],
                    "precio": p, "rebanado": prod_sel.get("rebanado", False),
                }
                estado["estado"] = "esperando_cantidad_libra"
                _set_estado(numero_cliente, estado)
                return (f"¿Cuánto {prod_sel['nombre']} quieres?\n\n"
                        f"1. 1/4 libra (${p * 0.25:.0f} pesos)\n"
                        f"2. 1/2 libra (${p * 0.5:.0f} pesos)\n"
                        f"3. 1 libra (${p:.0f} pesos)\n"
                        f"4. 2 libras (${p * 2:.0f} pesos)")
            item = {
                "clave": clave_sel, "nombre": prod_sel["nombre"],
                "cantidad": 1, "texto": "1",
                "unidad": prod_sel["unidad"], "precio": prod_sel["precio"],
            }
            if prod_sel.get("rebanado"):
                estado["items"] = items
                estado["item_pendiente_rebanado"] = item
                estado["cola_rebanado"] = []
                estado["rebanado_origen"] = "pidiendo"
                estado["estado"] = "esperando_rebanado"
                _set_estado(numero_cliente, estado)
                return (f"¿Cómo quieres el {item['nombre']}?\n\n"
                        "1. Rebanado\n"
                        "2. En pieza")
            items.append(item)
            estado["items"] = items
            _set_estado(numero_cliente, estado)
            return _resumen(items, "\nEscribe otro producto, o *sí* para confirmar.")

        # Pre-check monto vs. cantidad para productos por libra
        aclaracion = _detectar_aclaracion_necesaria(mensaje, negocio.get("catalogo", {}))
        if aclaracion:
            n = aclaracion["numero"]
            precio_u = aclaracion["precio"]
            if aclaracion["tipo"] == "pesos":
                item = {
                    "clave": aclaracion["clave"], "nombre": aclaracion["nombre"],
                    "cantidad": n / precio_u, "texto": f"RD${int(n)}",
                    "unidad": "libra", "precio": n,
                }
                if aclaracion.get("rebanado"):
                    estado["items"] = items
                    estado["item_pendiente_rebanado"] = item
                    estado["cola_rebanado"] = []
                    estado["rebanado_origen"] = "pidiendo"
                    estado["estado"] = "esperando_rebanado"
                    _set_estado(numero_cliente, estado)
                    return (f"¿Cómo quieres el {aclaracion['nombre']}?\n\n"
                            "1. Rebanado\n"
                            "2. En pieza")
                items.append(item)
                estado["items"] = items
                _set_estado(numero_cliente, estado)
                return _resumen(items, "\nEscribe otro producto, o *sí* para confirmar.")
            else:  # ambiguo
                estado["item_pendiente_rebanado"] = aclaracion
                estado["estado"] = "esperando_aclaracion_unidad"
                _set_estado(numero_cliente, estado)
                return (f"¿Son {int(n)} libras o RD${int(n)} de {aclaracion['nombre']}?\n\n"
                        f"1. {int(n)} libras (${precio_u * n:.0f} pesos)\n"
                        f"2. RD${int(n)}")

        # Consulta de disponibilidad
        if _es_consulta(msg):
            disponibles, agotados = _parsear_productos(mensaje, negocio.get("catalogo", {}))
            if disponibles or agotados:
                lineas = []
                for _, prod, _, _ in disponibles:
                    suf = "/libra" if prod["unidad"] == "libra" else ""
                    lineas.append(f"Si tenemos {prod['nombre']} - ${prod['precio']} pesos{suf}")
                for nombre_ag in agotados:
                    lineas.append(f"Agotado: {nombre_ag}")
                return "\n".join(lineas) + "\n\n" + _menu(negocio)
            return _menu(negocio)

        # Texto libre
        disponibles, agotados = _parsear_productos(mensaje, negocio.get("catalogo", {}))

        if not disponibles:
            if agotados:
                return f"Eso está agotado ahorita.\n\n" + _menu(negocio)
            return _menu(negocio)

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
                items.append(item)

        if cola_rebanado:
            primero = cola_rebanado.pop(0)
            estado["items"] = items
            estado["item_pendiente_rebanado"] = primero
            estado["cola_rebanado"] = cola_rebanado
            estado["rebanado_origen"] = "pidiendo"
            estado["estado"] = "esperando_rebanado"
            _set_estado(numero_cliente, estado)
            return (f"¿Cómo quieres el {primero['nombre']}?\n\n"
                    "1. Rebanado\n"
                    "2. En pieza")

        estado["items"] = items
        _set_estado(numero_cliente, estado)
        respuesta = _resumen(items, "\nEscribe otro producto, o *sí* para confirmar.")
        if agotados:
            respuesta += f"\n\n(Nota: {', '.join(agotados)} está agotado y no se agregó.)"
        return respuesta

    # ── ESPERANDO CANTIDAD LIBRA ──
    if s == "esperando_cantidad_libra":
        item_info = estado.get("item_pendiente_rebanado") or {}
        nombre = item_info.get("nombre", "")
        precio_u = item_info.get("precio", 0)
        clave = item_info.get("clave")
        rebanado = item_info.get("rebanado", False)

        OPCIONES = [(0.25, "1/4 libra"), (0.5, "1/2 libra"), (1.0, "1 libra"), (2.0, "2 libras")]

        cantidad, texto, precio_item = None, None, None

        if msg.strip() in ("1", "2", "3", "4"):
            cantidad, texto = OPCIONES[int(msg.strip()) - 1]
            precio_item = precio_u * cantidad
        else:
            FRACCIONES = [
                ("tres cuartos", 0.75, "3/4 libra"), ("3/4", 0.75, "3/4 libra"),
                ("media", 0.5, "1/2 libra"), ("1/2", 0.5, "1/2 libra"),
                ("cuarto", 0.25, "1/4 libra"), ("1/4", 0.25, "1/4 libra"),
            ]
            for frase, val, label in FRACCIONES:
                if frase in msg:
                    cantidad, texto, precio_item = val, label, precio_u * val
                    break

            if cantidad is None:
                m = re.search(r'\b(\d+(?:\.\d+)?)\b', msg)
                if m:
                    n = float(m.group(1))
                    tipo = _clasificar_numero_libra(n)
                    if tipo == "libra":
                        cantidad = n
                        label = int(n) if n == int(n) else n
                        texto = f"{label} libra{'s' if n != 1 else ''}"
                        precio_item = precio_u * n
                    elif tipo == "pesos":
                        cantidad = n / precio_u
                        texto = f"RD${int(n)}"
                        precio_item = n
                    else:
                        return (f"¿Son {int(n)} libras o RD${int(n)} de {nombre}?\n\n"
                                f"1. {int(n)} libras (${precio_u * n:.0f} pesos)\n"
                                f"2. RD${int(n)}")

        if cantidad is None:
            return (f"¿Cuánto {nombre} quieres?\n\n"
                    f"1. 1/4 libra (${precio_u * 0.25:.0f} pesos)\n"
                    f"2. 1/2 libra (${precio_u * 0.5:.0f} pesos)\n"
                    f"3. 1 libra (${precio_u:.0f} pesos)\n"
                    f"4. 2 libras (${precio_u * 2:.0f} pesos)")

        item = {
            "clave": clave, "nombre": nombre,
            "cantidad": cantidad, "texto": texto,
            "unidad": "libra", "precio": precio_item,
        }

        if rebanado:
            estado["item_pendiente_rebanado"] = item
            estado["cola_rebanado"] = []
            estado["rebanado_origen"] = "pidiendo"
            estado["estado"] = "esperando_rebanado"
            _set_estado(numero_cliente, estado)
            return (f"¿Cómo quieres el {nombre}?\n\n"
                    "1. Rebanado\n"
                    "2. En pieza")

        items.append(item)
        estado["items"] = items
        estado["item_pendiente_rebanado"] = None
        estado["estado"] = "pidiendo"
        _set_estado(numero_cliente, estado)
        return _resumen(items, "\nEscribe otro producto, o *sí* para confirmar.")

    # ── ESPERANDO ACLARACIÓN UNIDAD ──
    if s == "esperando_aclaracion_unidad":
        item_info = estado.get("item_pendiente_rebanado") or {}
        n = item_info.get("numero")
        precio_u = item_info.get("precio", 0)
        nombre = item_info.get("nombre", "")
        clave = item_info.get("clave")
        rebanado = item_info.get("rebanado", False)

        if msg == "1" or "libra" in msg:
            cantidad = n
            label = int(n) if n == int(n) else n
            texto = f"{label} libra{'s' if n != 1 else ''}"
            precio_item = precio_u * n
        elif msg == "2" or any(p in msg for p in ["peso", "dop", "rd$"]):
            cantidad = n / precio_u
            texto = f"RD${int(n)}"
            precio_item = n
        else:
            return (f"¿Son {int(n)} libras o RD${int(n)} de {nombre}?\n\n"
                    f"1. {int(n)} libras (${precio_u * n:.0f} pesos)\n"
                    f"2. RD${int(n)}")

        item = {
            "clave": clave, "nombre": nombre,
            "cantidad": cantidad, "texto": texto,
            "unidad": "libra", "precio": precio_item,
        }

        if rebanado:
            estado["item_pendiente_rebanado"] = item
            estado["cola_rebanado"] = []
            estado["rebanado_origen"] = "pidiendo"
            estado["estado"] = "esperando_rebanado"
            _set_estado(numero_cliente, estado)
            return (f"¿Cómo quieres el {nombre}?\n\n"
                    "1. Rebanado\n"
                    "2. En pieza")

        items.append(item)
        estado["items"] = items
        estado["item_pendiente_rebanado"] = None
        estado["estado"] = "pidiendo"
        _set_estado(numero_cliente, estado)
        return _resumen(items, "\nEscribe otro producto, o *sí* para confirmar.")

    # ── ESPERANDO CONFIRMACION ──
    if s == "esperando_confirmacion":
        if any(re.search(r"\b" + p + r"\b", msg) for p in _CONFIRMAR) or msg == "1":
            estado["estado"] = "esperando_direccion"
            _set_estado(numero_cliente, estado)
            return ("A que direccion te enviamos?\n\n"
                    "Ejemplo: Calle Duarte 45, Los Jardines, Santo Domingo")
        if msg == "2":
            _del_estado(numero_cliente)
            return "Orden cancelada. Escribe el codigo del negocio cuando quieras pedir de nuevo."
        return _resumen(items, "\n1. Confirmar pedido\n2. Cancelar")

    # ── ESPERANDO DIRECCIÓN ──
    if s == "esperando_direccion":
        estado["direccion"] = mensaje
        estado["estado"] = "esperando_referencia"
        _set_estado(numero_cliente, estado)
        return ("Alguna referencia para encontrarte mas facil?\n\n"
                "1. Sin referencia\n"
                "O escribe la referencia (ej: al lado de la farmacia, casa azul).")

    # ── ESPERANDO REFERENCIA ──
    if s == "esperando_referencia":
        estado["referencia"] = "Sin referencia" if msg in ("1", "ninguna") else mensaje
        total = sum(i["precio"] for i in items)
        puesto = _siguiente_turno(codigo)

        _guardar_pedido(
            numero_cliente, codigo, items, total, puesto,
            estado["direccion"], estado["referencia"]
        )
        estado["estado"] = "pedido_enviado"
        _set_estado(numero_cliente, estado)

        cola = _get_cola(codigo)
        posicion = len(cola)

        if posicion == 1:
            pedido = _get_pedido(numero_cliente)
            _enviar_pedido_a_negocio(negocio["numero_negocio"], numero_cliente, pedido, twilio_send)

        antes = posicion - 1
        if antes == 0:
            pos_txt = f"✅ Pedido confirmado. Tu puesto es P-{puesto} — eres el primero!"
        else:
            s = "s" if antes > 1 else ""
            pos_txt = f"✅ Pedido confirmado. Tu puesto es P-{puesto} — hay {antes} persona{s} antes que tí."

        r  = pos_txt + "\n\n"
        r += "Tu pedido:\n"
        r += "\n".join(_fmt(i) for i in items)
        r += f"\n\nTotal: ${total:.0f} pesos"
        r += f"\nDireccion: {estado['direccion']}"
        r += f"\nReferencia: {estado['referencia']}"
        r += "\n\n1. Ajustar pedido\n2. Cancelar"
        return r

    # ── PEDIDO ENVIADO ──
    if s == "pedido_enviado":
        if "ajustar" in msg or msg == "1":
            estado["estado"] = "ajustando"
            _set_estado(numero_cliente, estado)
            return (_resumen(items) +
                    "\n\nQue quieres cambiar?\n\n"
                    "• *quitar* [producto] para eliminarlo\n"
                    "• escribe un producto para agregarlo\n"
                    "• *listo* para confirmar los cambios")
        if msg == "2":
            return _ejecutar_cancelacion(numero_cliente, codigo, negocio, twilio_send,
                                         notificar_negocio=True)
        return ("*Tu pedido esta pendiente.*\n\n"
                "1. Ajustar pedido\n"
                "2. Cancelar")

    # ── ESPERANDO REBANADO ──
    if s == "esperando_rebanado":
        item = estado.get("item_pendiente_rebanado") or {}
        nombre = item.get("nombre", "")

        if msg == "1" or any(p in msg for p in ["rebanado", "rebana", "rebanada"]):
            item["rebanado_pref"] = "rebanado"
        elif msg == "2" or any(p in msg for p in ["pieza", "entero", "entera", "sin rebanar"]):
            item["rebanado_pref"] = "en pieza"
        else:
            return (f"¿Cómo quieres el {nombre}?\n\n"
                    "1. Rebanado\n"
                    "2. En pieza")

        items.append(item)
        cola_reb = estado.get("cola_rebanado") or []

        if cola_reb:
            siguiente = cola_reb.pop(0)
            estado["items"] = items
            estado["item_pendiente_rebanado"] = siguiente
            estado["cola_rebanado"] = cola_reb
            _set_estado(numero_cliente, estado)
            return (f"¿Cómo quieres el {siguiente['nombre']}?\n\n"
                    "1. Rebanado\n"
                    "2. En pieza")

        origen = estado.get("rebanado_origen", "pidiendo")
        estado["items"] = items
        estado["item_pendiente_rebanado"] = None
        estado["cola_rebanado"] = None
        estado["rebanado_origen"] = None
        estado["estado"] = origen
        _set_estado(numero_cliente, estado)

        if origen == "ajustando":
            return _resumen(items, "\nSigue ajustando o escribe *1* para confirmar cambios.")
        return _resumen(items, "\nEscribe otro producto, o *sí* para confirmar.")

    # ── ESPERANDO DECISION (producto no disponible) ──
    if s == "esperando_decision":
        item = estado.get("item_sin_stock") or {}
        nombre = item.get("nombre", "ese producto")

        if "continuar" in msg or msg == "1":
            items = [i for i in items if i["clave"] != item.get("clave")]
            if not items:
                return _ejecutar_cancelacion(numero_cliente, codigo, negocio, twilio_send,
                                             notificar_negocio=False)

            pedido = _get_pedido(numero_cliente)
            total = sum(i["precio"] for i in items)
            execute("""
                UPDATE pedidos SET items = %s, total = %s
                WHERE numero_cliente = %s AND estado = 'pendiente'
            """, (Json(items), total, numero_cliente))
            estado["items"] = items
            estado["estado"] = "pedido_enviado"
            estado["item_sin_stock"] = None
            _set_estado(numero_cliente, estado)

            txt  = f"PEDIDO ACTUALIZADO de {numero_cliente} — se eliminó {nombre}\n\n"
            txt += "\n".join(_fmt(i) for i in items)
            txt += f"\n\nTotal: ${total:.0f} pesos"
            twilio_send(negocio["numero_negocio"], txt)
            return _resumen(items, "\n\nPedido actualizado. Tu orden sigue en camino.")

        if msg == "2":
            return _ejecutar_cancelacion(numero_cliente, codigo, negocio, twilio_send,
                                         notificar_negocio=True)

        return (f"Lo sentimos, *{nombre}* no está disponible.\n\n"
                f"1. Continuar sin {nombre}\n"
                "2. Cancelar pedido")

    # ── AJUSTANDO ──
    if s == "ajustando":
        if re.search(r"\blisto\b", msg) or msg == "1":
            total = sum(i["precio"] for i in items)
            execute("""
                UPDATE pedidos SET items = %s, total = %s
                WHERE numero_cliente = %s AND estado = 'pendiente'
            """, (Json(items), total, numero_cliente))
            estado["estado"] = "pedido_enviado"
            _set_estado(numero_cliente, estado)

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
            antes = len(items)
            items = [i for i in items if buscado not in _norm(i["nombre"])]
            if len(items) == antes:
                return f"No encontre '{buscado}' en tu pedido."
            if not items:
                estado["items"] = items
                _set_estado(numero_cliente, estado)
                return "Eliminaste todos los productos. Agrega algo o escribe *cancelar*."
            estado["items"] = items
            _set_estado(numero_cliente, estado)
            return _resumen(items, "\nSigue ajustando o escribe *1* para confirmar cambios.")

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
                    items.append(item)

            if cola_rebanado:
                primero = cola_rebanado.pop(0)
                estado["items"] = items
                estado["item_pendiente_rebanado"] = primero
                estado["cola_rebanado"] = cola_rebanado
                estado["rebanado_origen"] = "ajustando"
                estado["estado"] = "esperando_rebanado"
                _set_estado(numero_cliente, estado)
                return (f"¿Cómo quieres el {primero['nombre']}?\n\n"
                        "1. Rebanado\n"
                        "2. En pieza")

            estado["items"] = items
            _set_estado(numero_cliente, estado)
            return _resumen(items, "\nSigue ajustando o escribe *1* para confirmar cambios.")

        if agotados:
            return "Ese producto está agotado ahorita."

        return _resumen(items, "\nEscribe *quitar* [producto], agrega un producto, o *1* para confirmar.")

    return None


def manejar_negocio(numero_negocio, codigo_negocio, mensaje, twilio_send):
    msg = _norm(mensaje)

    # "no hay [producto]"
    m = re.match(r"no\s+hay\s+(.+)", msg)
    if m:
        buscado = m.group(1).strip()
        pedidos_pendientes = execute(
            "SELECT numero_cliente, items FROM pedidos WHERE codigo = %s AND estado = 'pendiente'",
            (codigo_negocio,), fetch="all"
        ) or []
        for pedido_row in reversed(pedidos_pendientes):
            cliente = pedido_row["numero_cliente"]
            for item in (pedido_row["items"] or []):
                if buscado in _norm(item["nombre"]):
                    estado = _get_estado(cliente)
                    if estado:
                        estado["estado"] = "esperando_decision"
                        estado["item_sin_stock"] = item
                        _set_estado(cliente, estado)
                    negocio = obtener_negocio(codigo_negocio)
                    twilio_send(
                        cliente,
                        f"Lo sentimos, *{item['nombre']}* no está disponible.\n\n"
                        f"1. Continuar sin {item['nombre']}\n"
                        "2. Cancelar pedido"
                    )
                    return f"Cliente notificado sobre {item['nombre']}."
        return "No encontre pedidos pendientes con ese producto."

    # "listo" → pedido despachado
    if re.search(r"\blisto\b", msg):
        cola = _get_cola(codigo_negocio)
        if not cola:
            return "No hay pedidos pendientes."

        cliente_actual = cola[0]
        estado_actual = _get_estado(cliente_actual)
        if estado_actual and estado_actual.get("estado") == "esperando_decision":
            return "El pedido actual tiene un producto pendiente de decisión del cliente. Espera su respuesta."

        pedido_actual = _get_pedido(cliente_actual)
        puesto_actual = pedido_actual.get("turno", "?") if pedido_actual else "?"

        twilio_send(cliente_actual, "🛵 Tu pedido está en camino!")
        execute(
            "UPDATE pedidos SET estado = 'despachado' WHERE numero_cliente = %s AND estado = 'pendiente'",
            (cliente_actual,)
        )
        _del_estado(cliente_actual)

        cola_actual = _get_cola(codigo_negocio)
        if not cola_actual:
            return "✅ No hay más pedidos por ahora."

        siguiente = cola_actual[0]
        pedido_sig = _get_pedido(siguiente)
        _enviar_pedido_a_negocio(numero_negocio, siguiente, pedido_sig, twilio_send, prefijo="SIGUIENTE PEDIDO")
        twilio_send(siguiente, "⏳ Tu pedido está siendo preparado")
        _notificar_posiciones(codigo_negocio, twilio_send)

        puesto_sig = pedido_sig.get("turno", "?") if pedido_sig else "?"
        return f"✅ Puesto #P-{puesto_actual} despachado. Enviando #P-{puesto_sig} al siguiente."

    return None
