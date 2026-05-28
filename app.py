# app.py - Bot de WhatsApp para Colmados V1.5
# Agrega función de quitar productos de la orden

import os
import threading
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv
from inventario import (buscar_producto, parsear_linea_multiple,
                        verificar_disponibilidad, reducir_inventario,
                        productos, buscar_por_alias, normalizar_texto)

load_dotenv()

app = Flask(__name__)

ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
DUEÑO         = os.getenv("DUENO_WHATSAPP")

client = Client(ACCOUNT_SID, AUTH_TOKEN)

ordenes_activas = {}
estados         = {}
cola_rebanado   = {}
timers          = {}

# Palabras para cancelar toda la orden
PALABRAS_CANCELAR = [
    "cancelar", "cancel", "no quiero", "salir", "exit",
    "para", "stop", "bye", "adios", "adiós", "chao",
    "nada", "olvidalo", "olvídalo", "dejalo", "déjalo"
]

# Palabras para quitar UN producto de la orden
PALABRAS_QUITAR = [
    # Formales
    "quitar", "quita", "quítame", "quitame", "quítalo", "quitalo",
    "quítala", "quitala",
    "eliminar", "elimina", "elimíname", "eliminame",
    "elimínalo", "eliminalo", "elimínala", "eliminala",
    "remover", "remove", "remueve",
    "borrar", "borra", "bórrame", "borrame", "bórralo", "borralo",
    "bórrala", "borrala",
    "sacar", "saca", "sácame", "sacame", "sácalo", "sacalo",
    "sácala", "sacala",
    "no quiero el", "no quiero la",
    "cancelar ese", "cancelar esa",
    # Coloquiales dominicanas
    "eso no", "esa no",
    "no mejor no",
    "mejor sin", "sin el", "sin la",
    "no va el", "no va la", "no va",
    "olvida el", "olvida la",
    "olvídalo", "olvidalo", "olvídala", "olvidala",
    "esperate", "espérate",
    "ah no espera",
    "mejor quita",
    "no no no",
    "nah quiero", "na quiero",
]

TIMEOUT_SEGUNDOS = 300


def limpiar_orden(numero_cliente):
    ordenes_activas[numero_cliente] = {"items": [], "direccion": "", "referencia": ""}
    estados[numero_cliente]         = "pidiendo"
    cola_rebanado[numero_cliente]   = []


def cancelar_por_timeout(numero_cliente):
    if estados.get(numero_cliente, "pidiendo") != "pidiendo" or \
       ordenes_activas.get(numero_cliente, {}).get("items"):
        try:
            client.messages.create(
                body=(
                    "⏰ Tu orden fue cancelada por inactividad.\n\n"
                    "Escribe *hola* cuando quieras pedir de nuevo. 😊"
                ),
                from_=TWILIO_NUMBER,
                to=numero_cliente
            )
        except Exception:
            pass
        limpiar_orden(numero_cliente)


def reiniciar_timer(numero_cliente):
    if numero_cliente in timers:
        timers[numero_cliente].cancel()
    timer = threading.Timer(TIMEOUT_SEGUNDOS, cancelar_por_timeout, args=[numero_cliente])
    timer.daemon = True
    timer.start()
    timers[numero_cliente] = timer


def detener_timer(numero_cliente):
    if numero_cliente in timers:
        timers[numero_cliente].cancel()
        del timers[numero_cliente]


def formato_item(item):
    unidad         = item.get("unidad", "unidad")
    cantidad_texto = item.get("cantidad_texto", str(item["cantidad"]))
    rebanado       = item.get("rebanado_texto", "")

    if unidad == "libra":
        if cantidad_texto.replace('.', '', 1).isdigit():
            val = float(cantidad_texto)
            cantidad_display = f"{cantidad_texto} libra" if val == 1 else f"{cantidad_texto} libras"
        else:
            cantidad_display = cantidad_texto
        linea = f"• {cantidad_display} de {item['nombre']}"
    else:
        linea = f"• {int(item['cantidad'])}x {item['nombre']}"

    if rebanado:
        linea += f" — {rebanado}"

    linea += f" - ${item['precio']:.0f} pesos"
    return linea


def mostrar_orden(numero_cliente):
    orden = ordenes_activas[numero_cliente]["items"]
    total = sum(i["precio"] for i in orden)
    texto = "🛒 *Tu orden hasta ahora:*\n"
    for item in orden:
        texto += formato_item(item) + "\n"
    texto += f"\n💰 Total: ${total:.0f} pesos\n\n"
    texto += "Escribe más productos, *confirmar* para pedir o *cancelar* para salir."
    return texto


def notificar_dueno(numero_cliente):
    orden      = ordenes_activas[numero_cliente]["items"]
    direccion  = ordenes_activas[numero_cliente]["direccion"]
    referencia = ordenes_activas[numero_cliente]["referencia"]
    total      = sum(i["precio"] for i in orden)

    texto = "🔔 *ORDEN NUEVA*\n\n"
    texto += "🛒 *Pedido:*\n"
    for item in orden:
        texto += formato_item(item) + "\n"
    texto += f"\n💰 Total: ${total:.0f} pesos\n"
    texto += f"📍 Dirección: {direccion}\n"
    texto += f"📌 Referencia: {referencia}\n"
    texto += f"📞 Cliente: {numero_cliente}"

    client.messages.create(body=texto, from_=TWILIO_NUMBER, to=DUEÑO)


def detectar_quitar(mensaje):
    """
    Detecta si el cliente quiere quitar un producto.
    Retorna el nombre del producto a quitar o None.
    """
    mensaje = mensaje.lower().strip()
    for palabra in sorted(PALABRAS_QUITAR, key=len, reverse=True):
        if palabra in mensaje:
            # Extrae el producto después de la palabra clave
            resto = mensaje.replace(palabra, "").strip()
            resto = resto.replace("el ", "").replace("la ", "").replace("los ", "").replace("las ", "").strip()
            if resto:
                return resto
            return ""  # Quiere quitar algo pero no especificó
    return None


def quitar_producto_orden(numero_cliente, texto_producto):
    """
    Busca y elimina un producto de la orden activa.
    Retorna (éxito, nombre_producto)
    """
    orden = ordenes_activas[numero_cliente]["items"]
    if not orden:
        return False, None

    # Si no especificó producto, quita el último
    if not texto_producto:
        item_eliminado = orden.pop()
        return True, item_eliminado["nombre"]

    # Busca el producto por alias
    texto_limpio = normalizar_texto(texto_producto)
    clave, _ = buscar_por_alias(texto_limpio)

    if clave:
        for i, item in enumerate(orden):
            if item["clave"] == clave:
                item_eliminado = orden.pop(i)
                return True, item_eliminado["nombre"]

    # Búsqueda por nombre aproximado
    for i, item in enumerate(orden):
        if texto_producto in item["nombre"].lower() or item["clave"] in texto_producto:
            item_eliminado = orden.pop(i)
            return True, item_eliminado["nombre"]

    return False, None


@app.route("/webhook", methods=["POST"])
def webhook():
    numero_cliente = request.form.get("From")
    mensaje        = request.form.get("Body", "").strip()
    mensaje_lower  = mensaje.lower().strip()

    resp = MessagingResponse()
    msg  = resp.message()

    if numero_cliente not in ordenes_activas:
        ordenes_activas[numero_cliente] = {"items": [], "direccion": "", "referencia": ""}

    estado_actual = estados.get(numero_cliente, "pidiendo")

    # ── CANCELAR TODA LA ORDEN ──
    if any(p in mensaje_lower for p in PALABRAS_CANCELAR):
        if estado_actual != "pidiendo" or ordenes_activas[numero_cliente]["items"]:
            limpiar_orden(numero_cliente)
            detener_timer(numero_cliente)
            msg.body(
                "❌ *Orden cancelada.*\n\n"
                "Escribe *hola* cuando quieras pedir de nuevo. 😊"
            )
            return str(resp)

    # ── QUITAR UN PRODUCTO ──
    if estado_actual == "pidiendo":
        texto_a_quitar = detectar_quitar(mensaje_lower)
        if texto_a_quitar is not None:
            orden = ordenes_activas[numero_cliente]["items"]
            if not orden:
                msg.body("No tienes productos en tu orden todavía. Escribe *hola* para ver el menú.")
                return str(resp)

            exito, nombre = quitar_producto_orden(numero_cliente, texto_a_quitar)
            if exito:
                orden_actualizada = ordenes_activas[numero_cliente]["items"]
                if orden_actualizada:
                    respuesta = f"🗑️ *{nombre}* eliminado de tu orden.\n\n"
                    respuesta += mostrar_orden(numero_cliente)
                else:
                    detener_timer(numero_cliente)
                    respuesta = (
                        f"🗑️ *{nombre}* eliminado.\n\n"
                        "Tu orden está vacía. Escribe *hola* para ver el menú."
                    )
                msg.body(respuesta)
            else:
                msg.body(
                    f"🤔 No encontré ese producto en tu orden.\n\n"
                    f"{mostrar_orden(numero_cliente)}"
                )
            return str(resp)

    # Reinicia timer si hay orden activa
    if estado_actual != "pidiendo" or ordenes_activas[numero_cliente]["items"]:
        reiniciar_timer(numero_cliente)

    # ── ESPERANDO REBANADO ──
    if estado_actual == "esperando_rebanado":
        cola = cola_rebanado.get(numero_cliente, [])
        if cola:
            item_actual = cola[0]
            rebanado = "Sí, rebanado" if any(p in mensaje_lower for p in ["si", "sí", "yes"]) else "No rebanado"
            item_actual["rebanado_texto"] = rebanado
            ordenes_activas[numero_cliente]["items"].append(item_actual)
            cola.pop(0)

            if cola:
                cola_rebanado[numero_cliente] = cola
                siguiente = cola[0]
                msg.body(
                    f"🔪 *¿Quieres el {siguiente['nombre']} rebanado?*\n\n"
                    f"Responde *SI* o *NO*"
                )
                return str(resp)

        cola_rebanado[numero_cliente] = []
        estados[numero_cliente] = "pidiendo"
        msg.body(mostrar_orden(numero_cliente))
        return str(resp)

    # ── ESPERANDO DIRECCIÓN ──
    if estado_actual == "esperando_direccion":
        ordenes_activas[numero_cliente]["direccion"] = mensaje
        estados[numero_cliente] = "esperando_referencia"
        msg.body(
            "📌 ¿Alguna referencia para encontrarte más fácil?\n\n"
            "Ejemplo: *Al lado de la farmacia*, *Frente al parque*, *Casa azul*\n\n"
            "Si no tienes referencia escribe *ninguna*."
        )
        return str(resp)

    # ── ESPERANDO REFERENCIA ──
    if estado_actual == "esperando_referencia":
        referencia = mensaje if mensaje_lower != "ninguna" else "Sin referencia"
        ordenes_activas[numero_cliente]["referencia"] = referencia
        estados[numero_cliente] = "pidiendo"

        orden = ordenes_activas[numero_cliente]["items"]
        total = sum(i["precio"] for i in orden)

        resumen = "✅ *Orden confirmada!*\n\n"
        resumen += "🛒 *Tu pedido:*\n"
        for item in orden:
            resumen += formato_item(item) + "\n"
            reducir_inventario(item["clave"], item["cantidad"])
        resumen += f"\n💰 *Total: ${total:.0f} pesos*\n"
        resumen += f"📍 *Dirección:* {ordenes_activas[numero_cliente]['direccion']}\n"
        resumen += f"📌 *Referencia:* {referencia}\n\n"
        resumen += "¡Tu orden está en camino pronto! 🛵"
        msg.body(resumen)

        notificar_dueno(numero_cliente)
        detener_timer(numero_cliente)
        limpiar_orden(numero_cliente)
        return str(resp)

    # ── ESTADO NORMAL: PIDIENDO ──

    # Saludo o menú
    if any(p in mensaje_lower for p in ["hola", "buenas", "menu", "menú", "que tienen", "qué tienen"]):
        menu = "👋 *Bienvenido al Colmado!*\n\n📋 *Nuestros productos:*\n\n"
        for clave, producto in productos.items():
            if producto["cantidad"] > 0:
                unidad = "/libra" if producto["unidad"] == "libra" else ""
                menu += f"• {producto['nombre']} - ${producto['precio']} pesos{unidad}\n"
        menu += "\n✍️ Puedes pedir varios productos a la vez.\n"
        menu += "Ejemplo: *2 presidente, media libra de salami, 1 libra de arroz*\n\n"
        menu += "Escribe *cancelar* en cualquier momento para salir."
        msg.body(menu)

    # Confirmar orden
    elif any(p in mensaje_lower for p in ["confirmar", "confirma", "si", "sí", "dale", "ok", "okay", "listo", "va", "adelante", "procede"]):
        orden = ordenes_activas[numero_cliente]["items"]
        if orden:
            estados[numero_cliente] = "esperando_direccion"
            reiniciar_timer(numero_cliente)
            msg.body(
                "📍 *¿A qué dirección te enviamos?*\n\n"
                "Escribe tu calle, número y sector.\n"
                "Ejemplo: *Calle Duarte #45, Los Jardines, Santo Domingo*"
            )
        else:
            msg.body("No tienes ninguna orden activa. Escribe *hola* para ver el menú.")

    # Buscar productos
    else:
        resultados = parsear_linea_multiple(mensaje_lower)

        if not resultados:
            msg.body("🤔 No encontré ese producto. Escribe *menu* para ver lo que tenemos disponible.")
            return str(resp)

        items_normales = []
        items_rebanado = []
        items_agotados = []

        for clave, producto, cantidad, cantidad_texto in resultados:
            if not verificar_disponibilidad(clave, cantidad):
                items_agotados.append(producto["nombre"])
                continue

            precio_total = producto["precio"] * cantidad
            item = {
                "clave":          clave,
                "nombre":         producto["nombre"],
                "precio":         precio_total,
                "cantidad":       cantidad,
                "cantidad_texto": cantidad_texto,
                "unidad":         producto["unidad"],
            }

            if producto["rebanado"]:
                items_rebanado.append(item)
            else:
                items_normales.append(item)

        for item in items_normales:
            ordenes_activas[numero_cliente]["items"].append(item)

        if items_agotados:
            agotados_txt = ", ".join(items_agotados)
            msg.body(f"❌ Lo sentimos, estos productos están agotados: *{agotados_txt}*")
            return str(resp)

        if items_rebanado:
            cola_rebanado[numero_cliente] = items_rebanado
            estados[numero_cliente] = "esperando_rebanado"
            primero = items_rebanado[0]
            cantidad_txt = primero.get("cantidad_texto", "")
            msg.body(
                f"🔪 *{primero['nombre']}* — {cantidad_txt}\n\n"
                f"¿Lo quieres rebanado?\n\n"
                f"Responde *SI* o *NO*"
            )
            return str(resp)

        if ordenes_activas[numero_cliente]["items"]:
            reiniciar_timer(numero_cliente)
            msg.body(mostrar_orden(numero_cliente))
        else:
            msg.body("🤔 No encontré ningún producto válido. Escribe *menu* para ver lo que tenemos.")

    return str(resp)


if __name__ == "__main__":
    app.run(debug=True, port=3000)