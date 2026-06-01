from negocio_router import detectar_codigo, es_admin, obtener_negocio

passed = 0
failed = 0

def check(etiqueta, resultado, esperado):
    global passed, failed
    ok = resultado == esperado
    estado = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"[{estado}] {etiqueta}")
    if not ok:
        print(f"       esperado: {esperado!r}")
        print(f"       obtenido: {resultado!r}")


# ── detectar_codigo ──
print("--- detectar_codigo ---")
casos_codigo = [
    ("CO1",       "CO1",  ""),
    ("ba1 hola",  "BA1",  "hola"),
    ("hola",      None,   "hola"),
    ("XY99 test", None,   "XY99 test"),
]
for mensaje, codigo_esp, resto_esp in casos_codigo:
    codigo, resto = detectar_codigo(mensaje)
    check(f"detectar_codigo({mensaje!r})", (codigo, resto), (codigo_esp, resto_esp))

# ── es_admin ──
print("\n--- es_admin ---")
co1 = obtener_negocio("CO1")   # pin = "1234"
ba1 = obtener_negocio("BA1")   # pin = "5678"

casos_admin = [
    ("admin 1234",   co1,  True,  "pin correcto CO1"),
    ("admin 5678",   ba1,  True,  "pin correcto BA1"),
    ("ADMIN 1234",   co1,  True,  "case-insensitive"),
    ("admin 9999",   co1,  False, "pin incorrecto"),
    ("admin",        co1,  False, "sin pin"),
    ("admin 1234 x", co1,  False, "texto extra despues del pin"),
    ("1234",         co1,  False, "sin palabra admin"),
]
for mensaje, negocio, esperado, descripcion in casos_admin:
    check(f"es_admin({mensaje!r}) — {descripcion}", es_admin(mensaje, negocio), esperado)

print(f"\n{passed}/{passed + failed} tests pasaron")
