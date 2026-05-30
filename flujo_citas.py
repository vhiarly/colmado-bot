import json
import re
from datetime import datetime, timedelta, date
from negocio_router import cargar_negocios, obtener_negocio

_estados_citas   = {}  # numero_cliente → {codigo, estado, servicio_clave, servicio, dia, nombre_dia, hora}
_sesiones_admin  = {}  # numero         → {codigo, expira, preguntando_numero_principal}

DIAS_ISO  = {0:"lunes", 1:"martes", 2:"miercoles", 3:"jueves", 4:"viernes", 5:"sabado", 6:"domingo"}
DIAS_ES   = {"lunes":0,"martes":1,"miercoles":2,"miércoles":2,
             "jueves":3,"viernes":4,"sabado":5,"sábado":5,"domingo":6}
DIAS_DISP = {0:"Lunes",1:"Martes",2:"Miercoles",3:"Jueves",4:"Viernes",5:"Sabado",6:"Domingo"}


# ── Persistencia ──────────────────────────────────────────────────────────────

def _guardar(datos):
    import negocio_router
    with open("negocios.json", "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    negocio_router._negocios_cache = datos


# ── Tiempo ────────────────────────────────────────────────────────────────────

def _hm(hora_str):
    """'HH:MM' → minutos desde medianoche"""
    h, m = hora_str.split(":")
    return int(h) * 60 + int(m)

def _mh(minutos):
    """minutos → 'HH:MM'  (con wrap a medianoche)"""
    return f"{(minutos // 60) % 24:02d}:{minutos % 60:02d}"

def _fmt12(hora_str):
    h, m = map(int, hora_str.split(":"))
    mer = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12}:{m:02d} {mer}"

def _parsear_hora(texto):
    """Extrae hora de texto libre: '5pm', '17:00', '5:30pm' → 'HH:MM'"""
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", texto.lower())
    if m:
        h, mins = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3) == "pm" and h != 12:
            h += 12
        elif m.group(3) == "am" and h == 12:
            h = 0
        return f"{h:02d}:{mins:02d}"
    m = re.search(r"(\d{1,2}):(\d{2})", texto)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None

def _proximo(nombre_dia):
    """Fecha del próximo día de semana por nombre (desde mañana)."""
    obj = DIAS_ES.get(nombre_dia.lower())
    if obj is None:
        return None
    hoy = date.today()
    for i in range(1, 8):
        f = hoy + timedelta(days=i)
        if f.weekday() == obj:
            return f.isoformat()
    return None


# ── Disponibilidad ────────────────────────────────────────────────────────────

def _bloqueada(negocio, fecha, hora_str, duracion_min):
    t0, t1 = _hm(hora_str), _hm(hora_str) + duracion_min
    for b in negocio.get("bloqueos", []):
        if b.get("fecha") != fecha:
            continue
        if _hm(b["desde"]) < t1 and _hm(b["hasta"]) > t0:
            return True
    for c in negocio.get("citas", []):
        if c.get("fecha") != fecha:
            continue
        ci = _hm(c["hora"])
        if ci < t1 and ci + c["duracion_minutos"] > t0:
            return True
    return False


def _horas_del_dia(negocio, fecha, duracion_min):
    dia_nombre = DIAS_ISO[datetime.strptime(fecha, "%Y-%m-%d").weekday()]
    h_dia = negocio.get("horario", {}).get(dia_nombre, {})
    if not h_dia.get("trabaja") or not h_dia.get("inicio"):
        return []
    ini = _hm(h_dia["inicio"])
    fin = _hm(h_dia["fin"])
    if fin <= ini:            # cruza medianoche (ej: 12:00 → 01:00)
        fin += 24 * 60
    slots = []
    t = ini
    while t + duracion_min <= fin:
        hora_str = _mh(t)
        if not _bloqueada(negocio, fecha, hora_str, duracion_min):
            slots.append(hora_str)
        t += 30
    return slots


def _dias_del_negocio(negocio, duracion_min):
    """Próximos 8 días que trabaja y tienen al menos una hora libre."""
    hoy = date.today()
    dias = []
    for i in range(1, 9):
        f = hoy + timedelta(days=i)
        nombre = DIAS_ISO[f.weekday()]
        if not negocio.get("horario", {}).get(nombre, {}).get("trabaja"):
            continue
        if _horas_del_dia(negocio, f.isoformat(), duracion_min):
            dias.append((f.isoformat(), DIAS_DISP[f.weekday()], f.strftime("%d/%m")))
    return dias


# ── Textos ────────────────────────────────────────────────────────────────────

def _txt_servicios(negocio):
    lineas = [f"Bienvenido a {negocio['nombre']}!\n\nNuestros servicios:\n"]
    for i, (_, s) in enumerate(negocio.get("servicios", {}).items(), 1):
        if s.get("activo", True):
            lineas.append(f"{i}. {s['nombre']} - ${s['precio']} pesos ({s['duracion_minutos']} min)")
    lineas += ["", "Escribe el numero del servicio.", "Escribe cancelar para salir."]
    return "\n".join(lineas)


def _txt_dias(negocio, duracion_min):
    dias = _dias_del_negocio(negocio, duracion_min)
    if not dias:
        return None
    lineas = ["Dias disponibles:\n"]
    for i, (_, nombre, display) in enumerate(dias, 1):
        lineas.append(f"{i}. {nombre} {display}")
    lineas.append("\nEscribe el numero del dia.")
    return "\n".join(lineas)


def _txt_horas(negocio, fecha, duracion_min):
    horas = _horas_del_dia(negocio, fecha, duracion_min)
    if not horas:
        return None
    lineas = ["Horas disponibles:\n"]
    for i, h in enumerate(horas, 1):
        lineas.append(f"{i}. {_fmt12(h)}")
    lineas.append("\nEscribe el numero de la hora.")
    return "\n".join(lineas)


# ── Admin: autenticación ──────────────────────────────────────────────────────

def _buscar_por_pin(mensaje):
    """Retorna (codigo, negocio) si el mensaje es 'admin {pin}' de algún negocio de citas."""
    datos = cargar_negocios()
    for cod, neg in datos["negocios"].items():
        if neg.get("modo") != "citas":
            continue
        if re.match(r"^admin\s+" + re.escape(neg["pin"]) + r"$", mensaje.strip(), re.IGNORECASE):
            return cod, neg
    return None, None


def _codigo_admin(numero):
    """Retorna codigo si el número tiene acceso admin activo (registrado o sesión)."""
    datos = cargar_negocios()
    for cod, neg in datos["negocios"].items():
        if neg.get("modo") == "citas" and neg.get("numero_negocio") == numero:
            return cod
    sesion = _sesiones_admin.get(numero)
    if sesion and datetime.now() < sesion["expira"]:
        return sesion["codigo"]
    return None


def tiene_sesion_admin_citas(numero):
    """True si el número tiene una sesión admin activa (no incluye números registrados)."""
    s = _sesiones_admin.get(numero)
    return bool(s and datetime.now() < s["expira"])


# ── Flujo cliente ─────────────────────────────────────────────────────────────

def tiene_flujo_citas(numero_cliente):
    return numero_cliente in _estados_citas


def manejar_cita(numero_cliente, codigo, mensaje, twilio_send):
    """
    Maneja el flujo completo de agendado de citas.
    Retorna str (respuesta al cliente) o None.
    """
    msg = mensaje.strip().lower()

    if numero_cliente in _estados_citas:
        codigo = _estados_citas[numero_cliente]["codigo"]
    elif not codigo:
        return None

    negocio = obtener_negocio(codigo)
    if not negocio or negocio.get("modo") != "citas":
        return None

    if numero_cliente not in _estados_citas:
        _estados_citas[numero_cliente] = {"codigo": codigo, "estado": "inicio"}

    estado = _estados_citas[numero_cliente]
    s = estado["estado"]

    # Cancelar en cualquier momento
    if re.search(r"\b(cancelar|cancel|salir|bye|chao)\b", msg):
        _estados_citas.pop(numero_cliente, None)
        return "Reserva cancelada. Escribe el codigo del negocio cuando quieras agendar."

    # ── INICIO / ESPERANDO SERVICIO ──
    if s in ("inicio", "esperando_servicio"):
        estado["estado"] = "esperando_servicio"
        if not msg:
            return _txt_servicios(negocio)

        servicios = [(c, sv) for c, sv in negocio.get("servicios", {}).items()
                     if sv.get("activo", True)]
        elegido = None
        if msg.isdigit():
            idx = int(msg) - 1
            if 0 <= idx < len(servicios):
                elegido = servicios[idx]
        else:
            for c, sv in servicios:
                if msg in sv["nombre"].lower() or msg == c:
                    elegido = (c, sv)
                    break

        if not elegido:
            return _txt_servicios(negocio)

        clave_s, serv_s = elegido
        estado.update({"estado": "esperando_dia", "servicio_clave": clave_s, "servicio": serv_s})

        txt = _txt_dias(negocio, serv_s["duracion_minutos"])
        if not txt:
            _estados_citas.pop(numero_cliente, None)
            return "No hay disponibilidad en los proximos dias. Intenta mas adelante."
        return txt

    # ── ESPERANDO DÍA ──
    if s == "esperando_dia":
        serv = estado["servicio"]
        dias = _dias_del_negocio(negocio, serv["duracion_minutos"])
        if not dias:
            _estados_citas.pop(numero_cliente, None)
            return "No hay disponibilidad en los proximos dias. Intenta mas adelante."

        elegido = None
        if msg.isdigit():
            idx = int(msg) - 1
            if 0 <= idx < len(dias):
                elegido = dias[idx]
        else:
            for d in dias:
                if msg in d[1].lower() or msg in d[2]:
                    elegido = d
                    break

        if not elegido:
            return _txt_dias(negocio, serv["duracion_minutos"])

        fecha, nombre_dia, _ = elegido
        estado.update({"estado": "esperando_hora", "dia": fecha, "nombre_dia": nombre_dia})

        txt = _txt_horas(negocio, fecha, serv["duracion_minutos"])
        if not txt:
            estado["estado"] = "esperando_dia"
            return f"No hay horas disponibles el {nombre_dia}. Elige otro dia.\n\n" + _txt_dias(negocio, serv["duracion_minutos"])
        return txt

    # ── ESPERANDO HORA ──
    if s == "esperando_hora":
        serv  = estado["servicio"]
        fecha = estado["dia"]
        horas = _horas_del_dia(negocio, fecha, serv["duracion_minutos"])

        elegida = None
        if msg.isdigit():
            idx = int(msg) - 1
            if 0 <= idx < len(horas):
                elegida = horas[idx]
        else:
            for h in horas:
                if msg in h or msg in _fmt12(h).lower():
                    elegida = h
                    break

        if not elegida:
            return _txt_horas(negocio, fecha, serv["duracion_minutos"])

        estado.update({"estado": "confirmando", "hora": elegida})
        r  = "Resumen de tu cita:\n\n"
        r += f"Negocio:  {negocio['nombre']}\n"
        r += f"Servicio: {serv['nombre']}\n"
        r += f"Duracion: {serv['duracion_minutos']} min\n"
        r += f"Precio:   ${serv['precio']} pesos\n"
        r += f"Dia:      {estado['nombre_dia']}\n"
        r += f"Hora:     {_fmt12(elegida)}\n"
        r += "\nEscribe si para confirmar o cancelar para salir."
        return r

    # ── CONFIRMANDO ──
    if s == "confirmando":
        if not re.search(r"\b(si|sí|confirmar|confirma|dale|ok|listo)\b", msg):
            serv = estado["servicio"]
            return (f"Servicio: {serv['nombre']} — {_fmt12(estado['hora'])} el {estado['nombre_dia']}\n\n"
                    "Escribe si para confirmar o cancelar para salir.")

        serv  = estado["servicio"]
        cita  = {
            "numero_cliente":   numero_cliente,
            "servicio":         estado["servicio_clave"],
            "nombre_servicio":  serv["nombre"],
            "fecha":            estado["dia"],
            "hora":             estado["hora"],
            "duracion_minutos": serv["duracion_minutos"],
        }
        datos = cargar_negocios()
        datos["negocios"][codigo]["citas"].append(cita)
        _guardar(datos)

        twilio_send(
            negocio["numero_negocio"],
            f"NUEVA CITA\n\n"
            f"Servicio: {serv['nombre']}\n"
            f"Dia:      {estado['nombre_dia']} — {_fmt12(estado['hora'])}\n"
            f"Cliente:  {numero_cliente}"
        )

        _estados_citas.pop(numero_cliente, None)
        return (f"Cita confirmada!\n\n"
                f"Servicio: {serv['nombre']}\n"
                f"Dia:      {estado['nombre_dia']}\n"
                f"Hora:     {_fmt12(estado['hora'])}\n\n"
                f"Te esperamos en {negocio['nombre']}.")

    return None


# ── Flujo negocio ─────────────────────────────────────────────────────────────

def manejar_negocio_citas(numero, mensaje, twilio_send):
    """
    Maneja mensajes del negocio o de admin con pin.
    Retorna str (respuesta) o None.
    """
    msg      = mensaje.strip()
    msg_low  = msg.lower()

    # ── Pin desde número nuevo ──
    codigo_pin, neg_pin = _buscar_por_pin(msg)
    codigo_activo = _codigo_admin(numero)

    if not codigo_activo and codigo_pin:
        _sesiones_admin[numero] = {
            "codigo":  codigo_pin,
            "expira":  datetime.now() + timedelta(hours=48),
            "preguntando_numero_principal": True,
        }
        return (f"Acceso concedido a {neg_pin['nombre']}.\n"
                f"Tu sesion dura 48 horas.\n\n"
                f"Quieres hacer este numero el principal del negocio?\n"
                f"Responde si o no.")

    # ── Respuesta a pregunta de número principal ──
    sesion = _sesiones_admin.get(numero, {})
    if sesion.get("preguntando_numero_principal") and codigo_activo:
        sesion["preguntando_numero_principal"] = False
        if re.search(r"\b(si|sí)\b", msg_low):
            datos = cargar_negocios()
            datos["negocios"][codigo_activo]["numero_negocio"] = numero
            _guardar(datos)
            return "Listo. Este numero es ahora el numero principal del negocio."
        return "OK. Puedes usar los comandos de agenda normalmente."

    if not codigo_activo:
        return None

    codigo  = codigo_activo
    negocio = obtener_negocio(codigo)
    hoy     = date.today().isoformat()

    # ── mis citas hoy ──
    if re.search(r"mis\s+citas\s+hoy", msg_low):
        citas = sorted(
            [c for c in negocio.get("citas", []) if c["fecha"] == hoy],
            key=lambda c: c["hora"]
        )
        if not citas:
            return "No hay citas para hoy."
        lineas = [f"Citas de hoy:\n"]
        for c in citas:
            lineas.append(f"• {_fmt12(c['hora'])} — {c['nombre_servicio']} ({c['numero_cliente']})")
        return "\n".join(lineas)

    # ── mis citas semana ──
    if re.search(r"mis\s+citas\s+semana", msg_low):
        hoy_dt = date.today()
        fechas = {(hoy_dt + timedelta(days=i)).isoformat() for i in range(7)}
        citas  = sorted(
            [c for c in negocio.get("citas", []) if c["fecha"] in fechas],
            key=lambda c: (c["fecha"], c["hora"])
        )
        if not citas:
            return "No hay citas esta semana."
        lineas = ["Citas de la semana:\n"]
        for c in citas:
            lineas.append(f"• {c['fecha']} {_fmt12(c['hora'])} — {c['nombre_servicio']} ({c['numero_cliente']})")
        return "\n".join(lineas)

    # ── ocupado hasta HH:MM ──
    if "ocupado hasta" in msg_low:
        hasta = _parsear_hora(msg_low)
        if not hasta:
            return "No entendi la hora. Ejemplo: ocupado hasta 5pm"
        ahora = datetime.now().strftime("%H:%M")
        if _hm(hasta) <= _hm(ahora):
            return "Esa hora ya paso. Hasta que hora quieres bloquear?"
        datos = cargar_negocios()
        datos["negocios"][codigo]["bloqueos"].append(
            {"fecha": hoy, "desde": ahora, "hasta": hasta}
        )
        _guardar(datos)
        return f"Agenda bloqueada desde ahora hasta las {_fmt12(hasta)}."

    # ── no disponible (resto del día) ──
    if re.search(r"\bno\s+disponible\b", msg_low):
        ahora = datetime.now().strftime("%H:%M")
        datos = cargar_negocios()
        datos["negocios"][codigo]["bloqueos"].append(
            {"fecha": hoy, "desde": ahora, "hasta": "23:59"}
        )
        _guardar(datos)
        return "Agenda bloqueada por el resto del dia."

    # ── libre [dia] → bloquear ese día ──
    m = re.search(r"libre\s+(\w+)", msg_low)
    if m:
        nombre_dia = m.group(1)
        fecha_bloqueo = _proximo(nombre_dia)
        if not fecha_bloqueo:
            return f"No reconoci el dia '{nombre_dia}'."
        datos = cargar_negocios()
        datos["negocios"][codigo]["bloqueos"].append(
            {"fecha": fecha_bloqueo, "desde": "00:00", "hasta": "23:59"}
        )
        _guardar(datos)
        return f"{nombre_dia.capitalize()} {fecha_bloqueo} bloqueado completamente."

    return None
