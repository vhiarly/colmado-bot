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
    msg = _norm(mensaje)
    encontrados = []
    for clave, prod in catalogo.items():
        if not prod.get("activo", True) or prod.get("cantidad", 1) <= 0:
            continue
        nombre_norm = _norm(prod["nombre"])
        if nombre_norm not in msg and clave not in msg:
            continue
        cantidad, texto = _extraer_cantidad(msg, nombre_norm, prod["unidad"])
        encontrados.append((clave, prod, cantidad, texto))
    return encontrados


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
    lineas += ["", "Escribe lo que quieres pedir.", "Escribe cancelar para salir."]
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
        if neg["numero_negocio"] == numero:
            return cod
    return None


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
        if s == "pedido_enviado":
            twilio_send(negocio["numero_negocio"],
                        f"El cliente {numero_cliente} cancelo su pedido.")
        _estados.pop(numero_cliente, None)
        _ordenes_pendientes.pop(numero_cliente, None)
        return "Orden cancelada. Escribe el codigo del negocio cuando quieras pedir de nuevo."

    # ── PIDIENDO ──
    if s == "pidiendo":
        if not msg or any(p in msg for p in ["hola", "buenas", "menu", "menú", "que tienen"]):
            return _menu(negocio)

        if any(re.search(r"\b" + p + r"\b", msg) for p in _CONFIRMAR):
            if not estado["items"]:
                return "No tienes productos en tu orden. Escribe menu para ver lo que tenemos."
            estado["estado"] = "esperando_confirmacion"
            return _resumen(estado["items"],
                            "\nEscribe si para confirmar o cancelar para salir.")

        resultados = _parsear_productos(mensaje, negocio.get("catalogo", {}))
        if not resultados:
            return "No encontre ese producto. Escribe menu para ver lo que tenemos."

        for clave, prod, cantidad, texto in resultados:
            estado["items"].append({
                "clave": clave, "nombre": prod["nombre"],
                "cantidad": cantidad, "texto": texto,
                "unidad": prod["unidad"], "precio": prod["precio"] * cantidad,
            })
        return _resumen(estado["items"], "\nEscribe mas productos o confirmar para pedir.")

    # ── ESPERANDO CONFIRMACION ──
    if s == "esperando_confirmacion":
        if any(re.search(r"\b" + p + r"\b", msg) for p in _CONFIRMAR):
            estado["estado"] = "esperando_direccion"
            return ("A que direccion te enviamos?\n\n"
                    "Ejemplo: Calle Duarte 45, Los Jardines, Santo Domingo")
        return _resumen(estado["items"],
                        "\nEscribe si para confirmar o cancelar para salir.")

    # ── ESPERANDO DIRECCIÓN ──
    if s == "esperando_direccion":
        estado["direccion"] = mensaje
        estado["estado"] = "esperando_referencia"
        return ("Alguna referencia para encontrarte mas facil?\n\n"
                "Ejemplo: Al lado de la farmacia, Casa azul\n\n"
                "Si no tienes, escribe ninguna.")

    # ── ESPERANDO REFERENCIA ──
    if s == "esperando_referencia":
        estado["referencia"] = mensaje if msg != "ninguna" else "Sin referencia"
        items = estado["items"]
        total = sum(i["precio"] for i in items)

        _ordenes_pendientes[numero_cliente] = {
            "codigo": codigo, "items": list(items), "total": total,
            "direccion": estado["direccion"], "referencia": estado["referencia"],
        }
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
        r += "\n\nPuedes escribir cancelar si cambias de opinion antes de que sea procesado."
        r += "\n\n*Recuerda: tu pedido sigue PENDIENTE hasta que el negocio lo confirme.*"
        return r

    # ── PEDIDO ENVIADO ──
    if s == "pedido_enviado":
        return ("*Tu pedido esta pendiente.*\n\n"
                "Escribe cancelar para cancelarlo antes de que sea procesado.")

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
        for cliente, pedido in list(_ordenes_pendientes.items()):
            if pedido["codigo"] != codigo_negocio:
                continue
            for item in pedido["items"]:
                if buscado in _norm(item["nombre"]):
                    twilio_send(
                        cliente,
                        f"Lo sentimos, *{item['nombre']}* no esta disponible en tu pedido.\n\n"
                        "El negocio te contactara para ajustar o cancelar."
                    )
                    return f"Cliente notificado sobre {item['nombre']}."
        return "No encontre pedidos pendientes con ese producto."

    # "listo" → pedido despachado
    if re.search(r"\blisto\b", msg):
        for cliente, pedido in list(_ordenes_pendientes.items()):
            if pedido["codigo"] == codigo_negocio:
                twilio_send(cliente, "Tu pedido esta en camino!")
                _ordenes_pendientes.pop(cliente, None)
                _estados.pop(cliente, None)
                return f"Pedido de {cliente} marcado como completado."
        return "No hay pedidos pendientes."

    return None
