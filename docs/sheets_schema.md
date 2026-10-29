# Google Sheets MVP Schema & Data Dictionary

El spreadsheet actúa como almacenamiento del MVP del bot de WhatsApp y tiene **dos pestañas obligatorias**: `raw_expenses` (gastos) y `usuarios` (control de acceso). Los nombres de columna están en `snake_case` y alineados con el diseño de la futura base PostgreSQL para facilitar la migración.

---

## Pestaña `raw_expenses` (registro de gastos)

Nombre configurable con `WORKSHEET_NAME`. Si la pestaña está **vacía**, el bot crea la fila de encabezados; si ya tiene otra estructura, **hay que reemplazarla** por esta (el bot escribe por nombre de columna y, si falta alguna de las obligatorias, se niega a guardar y lo informa en el log en vez de desalinear los datos). Las pestañas creadas antes de la función de edición solo necesitan **agregar la columna** `reply_message_id` al final.

Fila de encabezados exacta (minúsculas):

```
timestamp, message_id, user_phone_hash, fecha_gasto, monto, moneda, categoria, subcategoria, medio_de_pago, descripcion, reply_message_id
```

| Orden | Columna | Tipo lógico | Descripción | Ejemplo |
| :---: | :--- | :--- | :--- | :--- |
| 1 | `timestamp` | DATETIME | Fecha y hora de ingesta, en la zona horaria `TIMEZONE` (`YYYY-MM-DD HH:mm:ss`) | `2026-10-19 22:50:00` |
| 2 | `message_id` | TEXT | ID único del mensaje de Evolution API. Es la **llave de idempotencia**: evita registrar dos veces un mensaje reentregado, incluso después de reiniciar el bot | `3EB0A1B2C3D4E5F60001` |
| 3 | `user_phone_hash` | TEXT (64) | Hash SHA-256 del número del remitente. **El número nunca se escribe en claro** en esta pestaña; el hash permite filtrar los gastos de cada persona | `9f86d081884c7d65...` |
| 4 | `fecha_gasto` | DATE | Fecha real del gasto, resuelta por el LLM (por ejemplo, "ayer") | `2026-10-18` |
| 5 | `monto` | NUMERIC | Valor numérico con punto decimal | `4500.00` |
| 6 | `moneda` | CHAR(3) | Código ISO 4217 (`ARS` si el usuario no menciona otra) | `ARS`, `USD` |
| 7 | `categoria` | TEXT | Categoría macro, **estrictamente en español** | `Alimentación` |
| 8 | `subcategoria` | TEXT | Detalle granular extraído por el LLM | `Supermercado`, `Restaurante` |
| 9 | `medio_de_pago` | TEXT | Medio de pago, **estrictamente en español** y obligatorio | `Tarjeta de Débito` |
| 10 | `descripcion` | TEXT | Concepto o detalle del gasto | `Compra semanal de provisiones` |
| 11 | `reply_message_id` | TEXT | **Opcional.** ID del mensaje con el que el bot confirmó el gasto: permite corregirlo respondiendo *a esa confirmación* | `3EB0A1B2C3D4E5F60009` |

**Categorías permitidas:** `Alimentación`, `Transporte`, `Vivienda`, `Servicios`, `Salud`, `Entretenimiento`, `Educación`, `Compras`, `Otros`.

**Medios de pago permitidos:** `Efectivo`, `Tarjeta de Crédito`, `Tarjeta de Débito`, `Transferencia`, `Billetera Virtual`, `No Especificado` (cuando el usuario no menciona cómo pagó).

### Reglas de integridad
1. No usar caracteres especiales en los encabezados; el orden de las columnas puede variar y se pueden agregar columnas propias, pero las **10 primeras columnas son obligatorias**. `reply_message_id` es opcional: si falta, el bot igual funciona, pero solo se pueden corregir los gastos respondiendo al mensaje propio del usuario (no a la confirmación del bot).
2. Formatear `monto` como "Número" (sin símbolo de moneda).
3. `message_id`, `user_phone_hash` y `reply_message_id` se guardan como texto (el bot antepone un apóstrofe interno que Sheets no muestra) para que nunca se interpreten como números.
4. Los textos libres que empiezan con `=`, `+`, `-` o `@` se guardan con un apóstrofe inicial para evitar inyección de fórmulas.
5. Las filas anteriores a este esquema (sin `user_phone_hash`) no se atribuyen a ningún usuario: no aparecen en ningún reporte.

---

## Pestaña `usuarios` (control de acceso)

Nombre configurable con `USERS_WORKSHEET_NAME`, por defecto `usuarios`. El bot la consulta con cada número que le escribe por WhatsApp y **es el único lugar donde se aprueba o rechaza a una persona**. Debe existir antes de levantar el bot: si falta, el bot no puede verificar a nadie y rechaza todos los mensajes (falla cerrado).

Fila de encabezados (se compara sin distinguir mayúsculas):

```
Telefono, Nombre, Email, Estado, Fecha_Registro
```

| Orden | Columna | La escribe | Descripción | Ejemplo |
| :---: | :--- | :--- | :--- | :--- |
| 1 | `Telefono` | bot / admin | Número real con código de país. Se compara ignorando espacios, `+`, guiones y la diferencia `549`/`54` de móviles argentinos. Formatear la columna como "Texto sin formato". | `5491122334455` |
| 2 | `Nombre` | bot | Nombre y apellido informados durante el onboarding | `Lucía Fernández` |
| 3 | `Email` | bot | Correo informado en el onboarding (se pide preferentemente de Gmail). Es la cuenta con la que se comparten los reportes de Drive | `lucia@gmail.com` |
| 4 | `Estado` | **solo el admin** | `1` aprobado · `2` rechazado · vacío = pendiente | `1` |
| 5 | `Fecha_Registro` | bot | Timestamp que genera el backend al detectar el número por primera vez (`YYYY-MM-DD HH:mm:ss`, hora local) | `2026-10-22 18:30:00` |

Se pueden agregar columnas propias (por ejemplo `Notas`) y cambiar el orden; `Telefono` y `Estado` son obligatorias. Las pestañas creadas con la primera versión del esquema (`nombre_completo`, `fecha_alta`, `fecha_actualizacion`) siguen funcionando.

### Estados

| `Estado` | Significado | Qué hace el bot |
| :---: | :--- | :--- |
| `1` | Aprobado | Deriva al flujo de registro de gastos (texto, audio con Whisper, imagen con descripción) y a los reportes. |
| `2` | Rechazado | Responde un mensaje estándar de acceso denegado y corta el flujo. No procesa nada. |
| vacío | Pendiente | Inicia (o continúa) el onboarding y avisa que la cuenta requiere aprobación manual del administrador antes de poder registrar gastos. |

Cualquier otro valor en `Estado` (`3`, `aprobado`, texto libre…) se trata como vacío: **nunca otorga acceso**.

### Flujo de alta
1. Un número desconocido escribe → el bot lo **registra de inmediato** (fila nueva con `Telefono`, `Fecha_Registro` y `Estado` vacío) y le pide nombre y email (solo por texto).
2. A medida que la persona responde, el bot completa `Nombre` y `Email` en esa misma fila. Con los datos completos avisa que la cuenta está pendiente; mientras `Estado` siga vacío solo responde un recordatorio fijo.
3. El administrador revisa la fila y completa `Estado` con `1` o `2`. También puede pre-aprobar a alguien agregando una fila con su `Telefono` y `Estado` = `1`.
4. El cambio se aplica **al instante** si está instalado el [Apps Script](apps_script_usuarios.md); si no, en hasta `USER_CACHE_TTL_SECONDS` (60 s por defecto): el bot guarda la tabla en memoria para no agotar la cuota de lectura de la API de Sheets.

### Privacidad
La pestaña contiene teléfonos, nombres y emails en claro (necesarios para identificar a quien se aprueba): compartila solo con las personas que administran los accesos. Los emails se usan para compartir reportes: **el bot no verifica que la persona sea dueña de esa casilla**, así que el administrador debe confirmarlo al aprobar.
El bot escribe estos valores sin interpretarlos (`RAW`) y antepone `'` a textos que empiecen con `=`, `+`, `-` o `@`, para evitar inyección de fórmulas / CSV.

---

## ✏️ Edición y corrección de registros (modo edición por cita)

Un usuario aprobado puede corregir un gasto ya registrado **respondiendo (citando) un mensaje del bot**, sin crear una fila nueva:

> **Usuario:** Gasté 1500 en café.
> **Bot:** ✅ Se registró tu gasto de 1,500.00 ARS en Alimentación (Café) con fecha 2026-10-23.
> **Usuario:** *(responde citando el mensaje del bot)* Me equivoqué, eran 15000.
> **Bot:** ✏️ Registro modificado · Monto: 15,000.00 ARS (antes 1,500.00 ARS) …

Cómo funciona:

1. Evolution envía el ID del mensaje citado en `contextInfo.stanzaId` —**al nivel de `data`**, como hermano de `message`—; WhatsApp en crudo lo anida dentro del contenido (`extendedTextMessage`, `audioMessage`, …). El parser revisa ambos lugares y lo expone como `quoted_message_id`.
2. El bot busca la fila donde `message_id` **o** `reply_message_id` coincidan con ese ID, **filtrando siempre por el `user_phone_hash` del remitente**: una cita a un gasto ajeno no se encuentra y no se modifica nada.
3. El LLM recibe el registro actual y el mensaje de corrección (`EDIT_SYSTEM_PROMPT`) y devuelve el mismo contrato JSON cambiando solo lo que el usuario corrigió.
4. Se valida con el mismo Data Quality Gate (confianza ≥ 0.65, monto > 0, moneda ISO) y se actualizan **solo** las columnas de datos: `fecha_gasto`, `monto`, `moneda`, `categoria`, `subcategoria`, `medio_de_pago` y `descripcion`. Los campos que el usuario no menciona (moneda y fecha) se conservan tal como estaban, aunque el modelo devuelva otro valor.
5. La fila conserva su `message_id` (idempotencia) y su `timestamp` (hora de ingesta); el `reply_message_id` se reemplaza por el de la **última** confirmación, que es la que hay que citar para seguir corrigiendo.

Reglas y límites:

- Si el mensaje citado **no** corresponde a un gasto del remitente, el bot pide que aclare y **no** registra nada (nunca inventa una fila).
- Si el usuario escribe una corrección **sin citar** (*"me equivoqué, eran 155"*), el bot lo detecta, avisa que no registró nada y pide que responda citando la confirmación (o el mensaje original).
- Se recuerda solo la **última** confirmación de cada gasto: para corregirlo otra vez hay que citar esa, no una confirmación anterior.
- Si la corrección no aporta un dato concreto ("me equivoqué" sin decir qué), la confianza queda por debajo del umbral y el bot pide el dato faltante.
- Si el registro corregido es idéntico al guardado, el bot avisa que no hay nada para cambiar y **no escribe** en la hoja.
- No hay límite de antigüedad para editar.
- No se guarda un historial de versiones: el mensaje de WhatsApp muestra el "antes → después" de cada campo modificado.

---

## Reportes por demanda

Un usuario aprobado que pide *"quiero mi resumen"* (por texto o por audio) recibe por WhatsApp el link a un Google Spreadsheet **nuevo** con sus gastos:

1. El bot filtra `raw_expenses` por el `user_phone_hash` del remitente (nunca por otro dato: aislamiento estricto).
2. La cuenta de servicio crea el spreadsheet (en la carpeta `REPORTS_FOLDER_ID` si está configurada) y lo comparte en **solo lectura** con el `Email` registrado en `usuarios`.
3. El reporte incluye fecha, monto, moneda, categoría, subcategoría, medio de pago y descripción; nunca el `message_id` ni el hash.
4. Hay un enfriamiento de `REPORT_COOLDOWN_SECONDS` (5 minutos) entre reportes del mismo usuario, porque cada uno crea un archivo nuevo en Drive.

Requiere la **Google Drive API habilitada** en el proyecto de GCP de la cuenta de servicio.
