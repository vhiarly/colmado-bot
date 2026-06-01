# Wasapeame — CLAUDE.md
# Plataforma de WhatsApp bots para negocios locales — caso de uso inicial: colmados dominicanos
# Stack: Python · Flask · Twilio · Render

---

## 1. THINK BEFORE CODING

Antes de escribir cualquier código:

- Declara explícitamente las suposiciones que estás haciendo
- Si hay ambigüedad en el request, **pregunta primero** — no adivines
- Si existe una solución más simple que la que se pidió, dila antes de implementar
- Presenta el plan brevemente antes de tocar archivos

**Contexto del proyecto a tener en cuenta:**
- El bot maneja conversaciones de WhatsApp con estado por número de teléfono
- Los usuarios son dueños de colmados y sus clientes en Santo Domingo
- Twilio envía webhooks POST al endpoint `/webhook`
- El estado de conversación vive en memoria (diccionario Python por ahora)

---

## 2. SIMPLICITY FIRST

- No agregues features que no fueron pedidos explícitamente
- No construyas abstracciones para código de un solo uso
- No añadas manejo de errores para escenarios imposibles en este contexto
- Si el código puede ser más corto sin perder claridad, hazlo más corto
- **Test rápido:** ¿Lo aprobaría un dev senior sin decir "esto es demasiado"?

**Reglas específicas para este proyecto:**
- Los estados de conversación deben ser strings simples, no objetos complejos
- Los mensajes de WhatsApp deben ser texto plano (sin markdown, Twilio no lo renderiza)
- No uses async/await a menos que sea estrictamente necesario

---

## 3. SURGICAL CHANGES

- Toca **únicamente** los archivos y funciones que el request requiere
- Mantén el estilo existente aunque lo harías diferente
- Si notas un bug o código muerto no relacionado, **menciónalo** — no lo toques
- Cada línea modificada debe trazarse directamente al request

**Archivos críticos — no tocar sin pedirlo explícitamente:**
- La lógica de parsing de mensajes (cantidades, fracciones, rebanado)
- El sistema de timeout de conversaciones (5 minutos)
- El flujo de notificación al dueño del colmado
- Las credenciales y variables de entorno

---

## 4. GOAL-DRIVEN EXECUTION

En vez de seguir instrucciones vagas, trabaja con criterios de éxito claros.

**Ejemplos para este proyecto:**

| ❌ En vez de esto | ✅ Usa esto |
|---|---|
| "Arregla el bug del timeout" | "El timeout debe resetear exactamente a 5 min con cada mensaje del usuario. Verifica con los casos: mensaje nuevo, respuesta al menú, confirmación de pedido." |
| "Agrega base de datos" | "Los pedidos deben persistir entre reinicios del servidor. Un pedido debe poder recuperarse por número de teléfono." |
| "Mejora el menú" | "El menú debe mostrar solo productos con stock > 0. Un cliente que escriba '1' debe recibir el primer producto disponible." |

Para tareas de múltiples pasos:
1. Enuncia el plan brevemente
2. Define cómo se verá el éxito
3. Ejecuta
4. Confirma que el criterio se cumple

---

## 5. RESPONSE STYLE

- Responde en palabras mínimas, sin preámbulo
- Sin resumen al final de la respuesta
- Sin frases de relleno: "here is", "I will", "of course", "great", "sure"
- Empieza siempre directamente con la respuesta
- En edits de código: muestra solo las líneas cambiadas con 3 líneas de contexto, nunca el archivo completo
- Para tareas simples usa razonamiento mínimo

---

## 6. SESSION HANDOFF

- Al terminar cada sesión, escribe un resumen de máximo 200 tokens en `.claude/SESSION_[fecha].md`
- El resumen debe incluir: qué se construyó, qué quedó incompleto, y qué hacer primero la próxima sesión

---

## Contexto técnico rápido

```
Proyecto:    Wasapeame
Repo:        github.com/vhiarly/colmado-bot
Twilio:      +1 234 415 1415
Deploy:      Render (free tier)
Endpoint:    POST /webhook
Lenguaje:    Python 3 + Flask
Estado:      En memoria (dict por número de teléfono)
```

**Features activos:**
- Menú automático de productos
- Cantidades y fracciones (ej: "media libra de salami")
- Rebanado de productos
- Timeout de 5 minutos por conversación
- Notificación al dueño al confirmar pedido
- Dirección y referencia del cliente

**Pendiente (no implementar sin pedirlo):**
- Base de datos (SQLite / PostgreSQL)
- Sistema de turnos / cola
- Panel web central
- Verificación de WhatsApp Business

---

*Basado en Claude Code best practices — Boris Cherny, Anthropic*
