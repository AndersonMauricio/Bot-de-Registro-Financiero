# 📋 Backlog de Ingeniería de Datos (DE)

Este documento detalla los requerimientos de arquitectura, calidad de datos y entrega de reportes para evolucionar el MVP del bot financiero hacia un producto funcional para el cliente.

## Estado de implementación

| # | Requerimiento | Estado | Qué falta hacer a mano |
| :---: | :--- | :---: | :--- |
| 1 | Webhook de estado (Apps Script → FastAPI → invalidación de caché) | ✅ | Instalar el Apps Script, definir `SHEETS_WEBHOOK_SECRET` y exponer el bot por una URL pública. Guía: [apps_script_usuarios.md](apps_script_usuarios.md) |
| 2 | Onboarding extendido (email, preferentemente Gmail) | ✅ | Nada. El email **no se verifica**: el administrador debe confirmarlo al aprobar |
| 3 | Taxonomía y medio de pago en español | ✅ | Nada |
| 4 | Trazabilidad PII (`user_phone_hash` en cada fila) | ✅ | Nada |
| 5 | Data Serving: reportes por demanda en Drive | ✅ (probado con dobles, **no contra Google real**) | Habilitar la Drive API del proyecto de GCP; opcional `REPORTS_FOLDER_ID` |
| 6 | Esquema de las pestañas `raw_expenses` y `usuarios` | ✅ | Reemplazar los encabezados de `raw_expenses` por los nuevos: [sheets_schema.md](sheets_schema.md) |
| 7 | Edición y corrección de registros (update por cita) | ✅ | Agregar la columna `reply_message_id` a `raw_expenses` (o `ALTER TABLE` en PostgreSQL): [sheets_schema.md](sheets_schema.md) |
| 8 | Gastos múltiples por mensaje (batching) | ✅ | Nada. El `message_id` del lote se sufija `-0`, `-1`, … sin cambios de esquema |
| 9 | Degradación controlada ante caídas de servicios | ✅ | Opcional: ajustar `GROQ_TIMEOUT_SECONDS`, `STORAGE_TIMEOUT_SECONDS`, `HTTP_TIMEOUT_SECONDS` y `MAX_TRANSACTIONS_PER_MESSAGE` en el `.env` |
| 10 | Onboarding como máquina de estados (el LLM solo extrae, las respuestas son texto fijo) | ✅ | Nada |
| 11 | Mensaje de contingencia multimedia sin jerga técnica | ✅ | Nada. Los videos siguen ignorándose en silencio (el parser solo conoce texto, audio e imagen) |
| 12 | Detector de reportes por palabras clave | ✅ | Nada |
| 13 | Mensajes fuera de dominio (sin error técnico) | ✅ | Nada |

### Decisiones de implementación

- **Punto 1 – "eliminar solo ese registro":** el bot guarda la lista completa en una sola lectura, así que borrar un registro lo haría pasar por "desconocido". En su lugar marca ese número como vencido: su próxima consulta relee la hoja **una vez** (y de paso refresca al resto). Si esa relectura falla, **cierra el acceso** en vez de usar el dato viejo. El secreto es distinto al de Evolution.
- **Punto 3:** los enums aceptan variaciones de mayúsculas y tildes (`ALIMENTACION` → `Alimentación`) pero rechazan cualquier valor fuera de la taxonomía; los valores en inglés anteriores (`CASH`, `CREDIT_CARD`…) ya no son válidos. Un test verifica que enums, prompt y `data_contract.json` listen lo mismo.
- **Punto 5 – intercepción:** se hace en el pipeline de gastos **después** de obtener el texto (o la transcripción), para que *"quiero mi resumen"* dicho por audio no se registre como gasto. La detección es determinista (sin LLM): un pedido de reporte nunca lleva un monto.
- **Punto 6:** `Fecha_Registro` se genera cuando el bot ve el número por primera vez, y desaparece `fecha_actualizacion`. Las pestañas con los nombres de la versión anterior (`nombre_completo`, `fecha_alta`) siguen funcionando. `raw_expenses` se escribe por nombre de columna y, si falta alguna de las 10 obligatorias, el bot se niega a guardar en vez de desalinear datos. La idempotencia por `message_id` ahora sobrevive a reinicios.
- **Punto 7 – "update por cita":** la fila se identifica por `message_id` **o** por `reply_message_id` (el ID de la confirmación que envía el bot, capturado de la respuesta de `message/sendText`), siempre filtrada por `user_phone_hash`: no existe forma de editar un gasto ajeno aunque se conozca el ID. El `UPDATE` sobrescribe solo las 7 columnas de datos; `message_id`, `timestamp` y el hash no se tocan. Una cita que no resuelve **no** se registra como gasto nuevo: el bot pide aclaración.
- **Punto 7 – corrección sin cita:** una frase como *"me equivoqué, eran 155"* se detecta con una regla determinista (`edit_intent.py`, sin LLM) y se responde pidiendo que cite el mensaje: antes caía en la extracción normal y devolvía un error técnico de confianza. Además, la moneda y la fecha solo cambian si el mensaje las menciona explícitamente (guarda en el servicio, independiente del LLM).
- **Punto 8 – contrato tolerante:** `LLMExtractionResponse` expone `transactions` (lista) y mantiene `transaction` como vista legacy sincronizada. Así la rama de corrección, los few-shots viejos y los consumes externos siguen funcionando sin migración.
- **Punto 8 – validación y escritura por ítem:** las restricciones duras (monto > 0, moneda ISO, detalle no vacío) se movieron del modelo Pydantic al Data Quality Gate. Con el modelo estricto, un solo ítem inválido hacía fallar todo el mensaje; ahora el gate valida cada gasto y el lote escribe solo los válidos en una única operación.
- **Punto 8 – ids:** un gasto usa el `message_id` original; un lote usa `-0`, `-1`, … La idempotencia reconoce el id base y sus sufijos, así una reentrega del webhook no duplica nada.
- **Punto 9 – timeouts:** el SDK de Groq arranca con 600 s de espera; ahora LLM y Whisper usan `GROQ_TIMEOUT_SECONDS` (30 s) + `GROQ_MAX_RETRIES`, y todo se ejecuta con `run_blocking` (cota dura por llamada). Evolution usa `HTTP_TIMEOUT_SECONDS`.
- **Punto 9 – mensajes honestos:** se distingue "no se guardó nada" (falla de escritura) de "puede que no se haya guardado" (timeout de escritura: la cancelación de `asyncio` no detiene el hilo).

- **Punto 10 – FSM sin estado propio:** el estado del onboarding ya vive en la hoja `usuarios` (qué datos están cargados), así que `onboarding_reply` es una función pura de esos datos y no hace falta persistir nada más. La respuesta se elige **después** de combinar lo extraído con lo guardado: un mensaje sin datos nuevos repite lo que falta. `OnboardingLLMResponse` ya no tiene `reply`: si un modelo lo sigue escribiendo, la clave se descarta y jamás llega al usuario. Se mantiene la llamada al LLM aunque sea el primer contacto, porque el primer mensaje puede traer los datos ("Soy Lucía, lucia@example.com").
- **Punto 11 – dónde está el texto:** vive en `expense_service.py` (no en el router): el router solo decide por estado de acceso. Se responde únicamente a las imágenes **sin descripción**; con descripción se registra el texto como siempre.
- **Punto 12 – palabras clave con salvaguardas:** `report_intent.py` busca `resumen`, `reporte`, `informe`, `extracto`, `planilla`, `mis gastos`, `gastos del mes` y `cuánto gasté` (y variantes) en el texto normalizado, y sube el tope de palabras de 15 a 25. Para que un gasto real no se confunda con un pedido: (a) cualquier número que no sea un año ni un período ("últimos 30 días") descarta el pedido; (b) un verbo de compra (`pagué`, `compré`…) sin verbo de pedido lo descarta ("pagué el resumen de la tarjeta"); (c) una corrección ("me equivoqué en mis gastos") tampoco es un reporte. "Cuánto gasté" es en sí mismo el pedido.
- **Punto 13 – para que el gate pueda responder:** hasta ahora una respuesta del LLM sin gastos (`transactions: []`) o sin monto fallaba en Pydantic, `_complete_json` reintentaba con todos los modelos candidatos y el usuario recibía "error en el servidor de IA". Ahora `transactions` puede venir vacía y `amount` puede ser `null`: ambas llegan al `DataQualityGate`, que marca el resultado con `no_expense_detected`. El prompt de extracción indica explícitamente devolver `transactions: []` y `confidence_score: 0.0` ante un mensaje que no es un gasto, y `null` si falta el monto.
- **Punto 13 – umbral de "confianza muy baja":** `OUT_OF_DOMAIN_CONFIDENCE_THRESHOLD = 0.30`. Entre 0.30 y 0.65 (`MIN_CONFIDENCE_THRESHOLD`) sigue siendo un gasto real pero poco claro y se pide aclarar monto y concepto. Es un valor elegido, no medido: conviene ajustarlo con mensajes reales.
- **Punto 13 – lotes:** el mensaje educativo solo se muestra si **todos** los ítems fueron marcados; si alguno se registró, la respuesta detalla qué falló (por ejemplo, "Falta el monto del gasto").
- **Punto 13 – correcciones:** una corrección que el LLM devuelve vacía o sin monto sigue contestando con el mensaje de corrección ("No pude aplicar la corrección…"), no con el de fuera de dominio, que no tendría sentido ahí.

### Riesgos a tener presentes

- **El hash no es un secreto fuerte:** SHA-256 sin sal de un número de teléfono se revierte por fuerza bruta (hay pocos números posibles). Sirve para no exponer el teléfono a simple vista y para filtrar, pero no protege ante quien tenga acceso a la hoja y quiera descubrir un número. Una mejora posible es `HMAC-SHA256` con una clave del servidor.
- **El email no está verificado:** el reporte se comparte con la dirección que la persona escribió en el onboarding. Quien apruebe usuarios debería confirmar que pertenece a esa persona.
- **Cada reporte es un archivo nuevo en Drive** de la cuenta de servicio; se limita con un enfriamiento por usuario (`REPORT_COOLDOWN_SECONDS`), pero no se borran los viejos.
- **Registro al primer contacto:** cualquier número que escriba queda registrado en `usuarios` (una escritura en la hoja). No hay límite por número desconocido.
- **Las correcciones no se auditan:** la fila guarda solo el valor final. El "antes → después" queda en el mensaje de WhatsApp, no en la base. Si se necesita trazabilidad, el paso siguiente es una tabla de auditoría (`expense_versions`) o guardar `editado_en`/`editado_por`.
- **La edición no tiene ventana temporal:** el usuario puede reescribir un gasto de hace meses. Se decidió así para el MVP; un límite (por ejemplo 30 días) es una línea de configuración.
- **Citar la confirmación del bot depende de una columna nueva:** en una hoja sin `reply_message_id` solo se pueden corregir los gastos respondiendo al mensaje propio del usuario. La degradación es silenciosa (un warning en el log), nunca rompe el registro.
- **Se recuerda una sola confirmación por gasto:** `reply_message_id` se sobrescribe en cada corrección, así que citar una confirmación anterior no resuelve. Guardar un historial de IDs exigiría otra tabla (o un valor multi-valor), que se dejó fuera del MVP.
- **Un lote comparte una única confirmación:** por eso la corrección dentro de un lote pasa por una lista numerada y una selección pendiente en memoria (`PENDING_SELECTION_TTL_SECONDS`). Si el proceso se reinicia entre la pregunta y la respuesta, la selección se pierde y hay que volver a citar.
- **La selección pendiente vive en el proceso:** igual que la caché de usuarios, no se comparte entre workers. El bot ya exige un solo worker de uvicorn.
- **El lote es todo o nada en la escritura:** no hay transacción en Sheets, así que la escritura es un único `append_rows`; si falla, el usuario recibe "no se guardó nada" y puede reenviar sin riesgo de filas huérfanas.
- **La confianza es del mensaje, no del ítem:** `confidence_score` es global, así que un mensaje confuso invalida todos sus gastos aunque los montos se hayan leído bien.
- **Un saludo sigue costando una llamada al LLM:** el mensaje fuera de dominio se detecta en el Data Quality Gate, es decir, después de consultar a Groq. Filtrar saludos con una regla determinista ahorraría tokens, pero se dejó fuera para no rechazar gastos reales por error.
- **"mis gastos" es ambiguo:** sin monto ni verbo de compra se interpreta como un pedido de reporte, así que "anotá mis gastos de hoy" genera un reporte en lugar de registrar. Es el costo de relajar la detección; el enfriamiento (`REPORT_COOLDOWN_SECONDS`) acota el daño.
- **Los videos no tienen respuesta:** el parser ignora todo lo que no sea texto, audio o imagen, así que quien manda un video no recibe ningún aviso.

---

## 1. Arquitectura Event-Driven para Usuarios (Webhook de Estado)
**Objetivo:** Eliminar las lecturas repetitivas y programadas a Google Sheets para evitar agotar la cuota de la API, reaccionando en tiempo real a las aprobaciones manuales.
* **Google Apps Script (onEdit):** Crear un disparador (trigger) en la pestaña `usuarios` de la hoja de control. Si se detecta un cambio en la columna de estado de un número, el script enviará un `POST` al servidor.
* **FastAPI Endpoint:** Desarrollar una ruta interna (ej. `/api/v1/webhooks/sheets`) para recibir este evento.
* **Invalidación de Caché:** Al recibir el payload con el número modificado, el backend elimina exclusivamente ese registro de su memoria caché.

## 2. Onboarding Extendido
**Objetivo:** Capturar la información necesaria para el provisionamiento de reportes privados (Data Serving).
* **Actualización de Prompt (`ONBOARDING_SYSTEM_PROMPT`):** El LLM debe preguntar y extraer obligatoriamente una dirección de correo electrónico (preferentemente Gmail).
* **Persistencia:** Guardar este correo en la base de usuarios para utilizarlo posteriormente en la asignación de permisos de Google Drive.
* **Actualización (punto 10):** el LLM ya no pregunta ni redacta: solo extrae `full_name` y `email`, y las preguntas son texto fijo de una máquina de estados.

## 3. Taxonomía y Enriquecimiento de Datos (Calidad de Datos)
**Objetivo:** Normalizar la entrada para el futuro modelado dimensional (dbt) y la visualización (Superset).
* **Categorías en Español:** El esquema Pydantic y el prompt deben forzar al modelo a clasificar el gasto usando estrictamente una taxonomía en español (ej. Alimentación, Transporte, Vivienda, Salud, Entretenimiento, Educación, Compras, Servicios, Otros).
* **Registro del Método de Pago en Español:** Incorporar un nuevo campo obligatorio en el contrato JSON.
  * *Ejemplos permitidos:* Efectivo, Tarjeta de Crédito, Tarjeta de Débito, Transferencia, Billetera Virtual, No Especificado.

## 4. Trazabilidad PII (Preparación para Filtrado RLS)
**Objetivo:** Vincular criptográficamente cada fila de gasto con el cliente que lo generó sin exponer su número en texto plano.
* **Inyección de Backend:** FastAPI debe calcular el hash SHA-256 del número remitente y escribirlo en la fila junto con los datos financieros.

## 5. Data Serving: Reportes por Demanda
**Objetivo:** Devolver al usuario un resumen transaccional de sus gastos.
* **Detección de Intención:** El enrutador debe interceptar pedidos de reporte (ej. "Quiero mi resumen").
* **Aislamiento de Datos:** La consulta a la base de datos debe incluir un filtro estricto por `user_phone_hash`.
* **Entrega en Google Sheets (Provisionamiento Automático):**
  1. La Cuenta de Servicio crea un nuevo Spreadsheet en Drive con los datos filtrados.
  2. Utilizando la API de Drive, el sistema otorga permisos de **Solo Lectura** al email registrado en el onboarding.
  3. Evolution API envía la URL al usuario por WhatsApp.

## 6. Evolución del Esquema de Base de Datos (Google Sheets MVP)
**Objetivo:** Alinear las columnas de las hojas de cálculo con el diseño de la futura base de datos PostgreSQL, garantizando la recolección de métricas clave, idempotencia y privacidad.

**Pestaña `raw_expenses` (Registro de Gastos):**
Reemplazar la estructura actual por las siguientes columnas:
1. `timestamp`: Fecha y hora de ingesta.
2. `message_id`: ID único del mensaje de Evolution API (llave de idempotencia para evitar registros duplicados).
3. `user_phone_hash`: Hash SHA-256 del número del usuario (reemplaza la antigua columna "Telefono" por normas de privacidad PII).
4. `fecha_gasto`: Fecha inferida del gasto.
5. `monto`: Valor numérico.
6. `moneda`: Código ISO 4217 (ARS, USD).
7. `categoria`: Clasificación macro (estrictamente en español).
8. `subcategoria`: Nivel de detalle granular extraído por el LLM (ej. Supermercado, Restaurante).
9. `medio_de_pago`: Método utilizado para pagar (estrictamente en español).
10. `descripcion`: Concepto o detalle extraído.

**Pestaña `usuarios` (Control de Acceso):**
Estructura obligatoria para gestionar los accesos:
1. `Telefono`: Número real con código de país para la validación del enrutador de FastAPI.
2. `Nombre`: Capturado en el onboarding.
3. `Email`: Correo del cliente para otorgar permisos de lectura al reporte de Drive.
4. `Estado`: Variable de control (1 = Aprobado, 2 = Rechazado, Vacío = Pendiente).
5. `Fecha_Registro`: Timestamp generado por el backend al detectar el nuevo número.


## 7. Edición y Corrección de Registros (Update Mode por Cita)
**Objetivo:** Permitir corregir errores de tipeo o montos incorrectos en gastos ya registrados, sin duplicar filas ni ensuciar la base.

* **Detección en el Webhook:** Evolution envía el ID del mensaje citado en `contextInfo.stanzaId`. El parser lo expone como `quoted_message_id` y el pipeline de gastos entra en modo corrección en lugar de insertar.
* **Lógica de actualización:**
  1. Se busca la fila original por `message_id` o `reply_message_id`, **siempre** acotada al `user_phone_hash` del remitente (en Sheets se resuelve la fila A1 y se actualiza con `spreadsheets.values.batchUpdate`; en PostgreSQL, `UPDATE ... WHERE message_id = :id AND user_phone_hash = :hash`).
  2. El LLM recibe el registro original y el mensaje de corrección (`EDIT_SYSTEM_PROMPT`), devolviendo el mismo contrato JSON con solo los campos corregidos.
  3. La validación es la misma del alta (Data Quality Gate) y se sobrescriben únicamente las columnas de datos; la identidad de la fila (`message_id`, `user_phone_hash`) y el `timestamp` de ingesta quedan intactos.
* **Trazabilidad del hilo:** el bot responde citando el mensaje del usuario y guarda el ID de su propia respuesta en `reply_message_id`, de modo que la siguiente corrección puede citar esa confirmación.


## 8. Soporte para Gastos Múltiples (Batching)
**Objetivo:** Procesar notas de voz o textos largos con más de una transacción en un solo envío.

* **Contrato:** el LLM devuelve `transactions` (lista, maxItems `MAX_TRANSACTIONS_PER_MESSAGE`; puede venir vacía si el mensaje no es un gasto, ver punto 13) en lugar de un objeto único `transaction`; la clave vieja se acepta por compatibilidad.
* **Calidad de datos:** `DataQualityGate.validate_transactions` valida cada gasto por separado y asigna `message_id` = `<id>` para un gasto único o `<id>-0`, `<id>-1`, … para un lote.
* **Persistencia:** una sola escritura por lote (`append_rows` en Sheets, `INSERT` multi-fila en PostgreSQL); si falla no se guarda ninguna fila y el usuario lo sabe.
* **Confirmación numerada** y corrección por selección cuando la cita corresponde a un lote.

## 9. Graceful Degradation (Manejo de Errores y Contingencia)
**Objetivo:** Evitar fallas silenciosas: si un servicio externo se cae, el usuario debe saber que su gasto NO fue registrado.

* **Timeouts explícitos:** `GROQ_TIMEOUT_SECONDS` y `GROQ_MAX_RETRIES` en los SDK de Groq/Whisper, `HTTP_TIMEOUT_SECONDS` en Evolution y `STORAGE_TIMEOUT_SECONDS` en el repositorio.
* **Cota dura por llamada:** `run_blocking` envuelve cada llamada bloqueante con `asyncio.wait_for`, de modo que ninguna caída deja al usuario esperando.
* **Mensajes por clase de falla:** IA caída, base caída ("no se guardó nada"), timeout de escritura ("puede que no se haya guardado") y excepción no prevista (mensaje genérico del router con log contextual).

## 10. Onboarding como Máquina de Estados (FSM)
**Objetivo:** Reducir tokens, latencia y alucinaciones (errores de puntuación, promesas de aprobación) sacando al LLM de la redacción de respuestas.

* **`ONBOARDING_SYSTEM_PROMPT`:** sin la clave `reply`; el LLM devuelve estrictamente `{"collected": {"full_name": "...", "email": "..."}}`.
* **`onboarding_service.py`:** `onboarding_reply(data)` evalúa con `if/else` puros qué falta y devuelve un texto fijo:
  * Nada recolectado: "¡Hola! Para darte de alta y registrar tus gastos, necesito tu nombre, apellido y un email (preferentemente de Gmail)."
  * Falta el email: "Tengo tu nombre, pero me falta tu email. ¿Me lo pasás?"
  * Falta el nombre: "Tengo tu email, pero me falta tu nombre y apellido."
  * Completo: aviso fijo de aprobación manual (sin cambios).

## 11. Mensaje de Contingencia Multimedia
**Objetivo:** Eliminar la jerga técnica ("MVP") de cara al usuario final.

* **`expense_service.py`:** una imagen sin descripción recibe *"📸 Recibimos tu imagen. Por ahora no puedo leer fotos o videos automáticamente. Por favor, escribime el detalle y el monto, o mandame un audio."*

## 12. Detector de Reportes por Palabras Clave
**Objetivo:** Evitar que pedidos de resumen en lenguaje natural caigan en el pipeline de gastos y generen errores técnicos.

* **`report_intent.py`:** búsqueda por palabras clave (`resumen`, `reporte`, `mis gastos`, `cuánto gasté`…) en lugar de la frase estricta, con salvaguardas para no confundir un gasto con un pedido (ver decisiones del punto 12).

## 13. Mensajes Fuera de Dominio
**Objetivo:** Si el usuario hace una pregunta general ("¿Qué es un mvp?", "Hola bot"), no intentar extraer un gasto ni mostrar un error técnico.

* **`data_quality.py`:** si no hay gastos, falta el monto o la confianza es menor a 0.30, el resultado se marca con `no_expense_detected` y el bot responde *"No detecté ningún monto o gasto en tu mensaje. Recordá que solo puedo registrar transacciones financieras o generar tu resumen de gastos."*
* **Contrato:** `transactions` puede venir vacía y `amount` puede ser `null` (el prompt de extracción lo indica) para que la respuesta llegue al gate en lugar de fallar el parseo.
