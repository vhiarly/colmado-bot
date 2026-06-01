import os
import re
import threading
from flask import Flask, request, g
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv
from negocio_router import detectar_codigo, obtener_negocio
from flujo_pedidos import manejar_pedido, manejar_negocio, tiene_flujo_activo, es_numero_negocio, limpiar_flujo
from flujo_citas import manejar_cita, manejar_negocio_citas, tiene_flujo_citas, tiene_sesion_admin_citas
from asistente_ia import consultar_ia, respuesta_ayuda

load_dotenv()

app = Flask(__name__)


@app.after_request
def log_response(response):
    numero = getattr(g, "numero_cliente", "?")
    print(f"[OUT] {numero}: {response.get_data(as_text=True)!r}")
    return response


ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

client = Client(ACCOUNT_SID, AUTH_TOKEN)

timers           = {}
_clientes_vistos = set()

TIMEOUT_SEGUNDOS = 300

_PATRONES_NEGOCIO = {
    "pedidos": [r"^no\s+hay\b", r"\blisto\b"],
    "citas":   [r"mis\s+citas\s+(hoy|semana)", r"ocupado\s+hasta\b",
                r"\bno\s+disponible\b", r"\blibre\s+\w+"],
    "comun":   [r"^ayuda$", r"^admin\s+"],
}


def _es_comando_negocio(msg_lower, modo):
    patrones = _PATRONES_NEGOCIO.get(modo, []) + _PATRONES_NEGOCIO["comun"]
    return any(re.search(p, msg_lower) for p in patrones)


def detener_timer(numero_cliente):
    if numero_cliente in timers:
        timers[numero_cliente].cancel()
        del timers[numero_cliente]


def cancelar_por_timeout(numero_cliente):
    limpiar_flujo(numero_cliente)
    try:
        client.messages.create(
            body=(
                "Tu conversación expiró por inactividad.\n\n"
                "Escribe el código del negocio cuando quieras continuar."
            ),
            from_=TWILIO_NUMBER,
            to=numero_cliente
        )
    except Exception:
        pass


def reiniciar_timer(numero_cliente):
    if numero_cliente in timers:
        timers[numero_cliente].cancel()
    timer = threading.Timer(TIMEOUT_SEGUNDOS, cancelar_por_timeout, args=[numero_cliente])
    timer.daemon = True
    timer.start()
    timers[numero_cliente] = timer


@app.route("/webhook", methods=["POST"])
def webhook():
    numero_cliente = request.form.get("From")
    mensaje        = request.form.get("Body", "").strip()
    mensaje_lower  = mensaje.lower().strip()

    print(f"[IN]  {numero_cliente}: {mensaje!r}")
    g.numero_cliente = numero_cliente

    resp = MessagingResponse()
    msg  = resp.message()

    twilio_send = lambda to, body: client.messages.create(body=body, from_=TWILIO_NUMBER, to=to)

    # ── MENSAJES DE NEGOCIOS DEL ROUTER ──
    codigo_emisor = es_numero_negocio(numero_cliente)
    print(f"[DEBUG negocio] numero={numero_cliente} → codigo={codigo_emisor}")
    if codigo_emisor:
        neg_emisor  = obtener_negocio(codigo_emisor)
        modo_emisor = neg_emisor.get("modo") if neg_emisor else "pedidos"
        codigo_en_msg, _ = detectar_codigo(mensaje)

        if not codigo_en_msg and _es_comando_negocio(mensaje_lower, modo_emisor):
            if mensaje_lower.strip() == "ayuda":
                msg.body(respuesta_ayuda(modo_emisor))
                return str(resp)

            if modo_emisor == "citas":
                resultado = manejar_negocio_citas(numero_cliente, mensaje, twilio_send)
            else:
                resultado = manejar_negocio(numero_cliente, codigo_emisor, mensaje, twilio_send)

            if resultado is None:
                resultado = consultar_ia(codigo_emisor, modo_emisor, mensaje)

            if resultado:
                msg.body(resultado)
            return str(resp)

    # ── ADMIN CITAS ──
    if re.match(r"^admin\s+", mensaje_lower) or tiene_sesion_admin_citas(numero_cliente):
        resultado = manejar_negocio_citas(numero_cliente, mensaje, twilio_send)
        if resultado:
            msg.body(resultado)
        return str(resp)

    # ── ROUTER DE NEGOCIOS (clientes) ──
    codigo, resto = detectar_codigo(mensaje)
    if codigo or tiene_flujo_activo(numero_cliente) or tiene_flujo_citas(numero_cliente):
        msg_flujo = resto if codigo else mensaje
        if codigo:
            neg_tmp = obtener_negocio(codigo)
            modo = neg_tmp.get("modo") if neg_tmp else "pedidos"
        else:
            modo = "citas" if tiene_flujo_citas(numero_cliente) else "pedidos"
        if modo == "citas":
            respuesta = manejar_cita(numero_cliente, codigo, msg_flujo, twilio_send)
        else:
            respuesta = manejar_pedido(numero_cliente, codigo, msg_flujo, twilio_send)
            if tiene_flujo_activo(numero_cliente):
                reiniciar_timer(numero_cliente)
            else:
                detener_timer(numero_cliente)
        if respuesta:
            msg.body(respuesta)
        return str(resp)

    # ── SIN CÓDIGO ──
    if numero_cliente not in _clientes_vistos:
        _clientes_vistos.add(numero_cliente)
        msg.body(
            "Bienvenido a *Wasapeame!* 🌿\n\n"
            "Somos la plataforma de WhatsApp para negocios locales — pedidos, citas y más.\n\n"
            "Para continuar, escribe el *código del negocio* al inicio de tu mensaje.\n\n"
            "Ejemplo: escribe *CO1 hola* para conectarte con un negocio.\n\n"
            "Si no tienes el código, pídelo directamente al negocio."
        )
        return str(resp)

    msg.body("Escribe el *código del negocio* para continuar. Si no lo tienes, pídelo al negocio.")
    return str(resp)


if __name__ == "__main__":
    app.run(debug=True, port=3000)
