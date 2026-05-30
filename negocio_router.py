import json
import re

_negocios_cache = None

def cargar_negocios():
    global _negocios_cache
    if _negocios_cache is None:
        with open("negocios.json", encoding="utf-8") as f:
            _negocios_cache = json.load(f)
    return _negocios_cache

def detectar_codigo(mensaje):
    """Retorna (codigo, resto_mensaje) si el mensaje empieza con un código válido (ej: CO1), o (None, mensaje)."""
    match = re.match(r'^([A-Z]{2}\d+)\s*(.*)', mensaje.strip(), re.IGNORECASE)
    if not match:
        return None, mensaje
    codigo = match.group(1).upper()
    datos = cargar_negocios()
    if codigo in datos["negocios"]:
        return codigo, match.group(2).strip()
    return None, mensaje

def obtener_negocio(codigo):
    datos = cargar_negocios()
    return datos["negocios"].get(codigo.upper())

def es_admin(mensaje, negocio):
    """Retorna True si el mensaje es exactamente 'admin <pin>'."""
    patron = re.compile(r'^admin\s+' + re.escape(negocio["pin"]) + r'$', re.IGNORECASE)
    return bool(patron.match(mensaje.strip()))

def obtener_modo(codigo):
    negocio = obtener_negocio(codigo)
    if negocio:
        return negocio.get("modo")
    return None
