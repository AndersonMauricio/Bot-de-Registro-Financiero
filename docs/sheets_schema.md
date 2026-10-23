# Google Sheets MVP Schema & Data Dictionary

Esta hoja de cálculo actúa como almacenamiento temporal (Staging) para el MVP del bot de WhatsApp.
Los nombres de columna están normalizados en `snake_case` estricto para facilitar la posterior ingesta y migración a SQL.

## Columnas del MVP

| Orden | Nombre Columna | Tipo Lógico | Validación / Formato | Ejemplo |
| :---: | :--- | :--- | :--- | :--- |
| 1 | `timestamp` | DATETIME | Fecha y hora exacta del registro (`YYYY-MM-DD HH:mm:ss`) | `2026-10-19 22:50:00` |
| 2 | `monto` | NUMERIC(12,2)| Valor numérico puro (decimal con punto) | `4500.00` |
| 3 | `moneda` | CHAR(3) | Código de moneda ISO 4217 | `ARS`, `USD` |
| 4 | `categoria` | VARCHAR | Agrupación general | `Supermercado`, `Comida`, `Transporte` |
| 5 | `descripcion` | TEXT | Detalle específico de la compra | `Compra semanal de provisiones` |

## Reglas de Integridad en Google Sheets
1. No usar caracteres especiales en los encabezados.
2. Formatear la columna `amount` como "Número" (sin símbolo de moneda para evitar strings mezclados).
3. Formatear `transaction_date` como "Fecha" (`YYYY-MM-DD`).

---

## Pestaña `usuarios` (control de acceso)

Pestaña del mismo spreadsheet (nombre configurable con `USERS_WORKSHEET_NAME`, por defecto `usuarios`).
El bot la consulta con cada número que le escribe por WhatsApp y **es el único lugar donde se aprueba o rechaza a una persona**.
Debe existir antes de levantar el bot: si falta, el bot no puede verificar a nadie y rechaza todo (falla cerrado).

| Orden | Columna | La escribe | Descripción | Ejemplo |
| :---: | :--- | :--- | :--- | :--- |
| 1 | `telefono` | bot / admin | Número con código de país. Se compara ignorando espacios, `+`, guiones y la diferencia `549`/`54` de móviles argentinos. Formatear la columna como "Texto sin formato". | `5491122334455` |
| 2 | `estado` | **solo el admin** | `1` aprobado · `2` rechazado · vacío = desconocido / pendiente. | `1` |
| 3 | `nombre_completo` | bot | Nombre y apellido informados durante el onboarding. | `Lucía Fernández` |
| 4 | `email` | bot | Email informado durante el onboarding. | `lucia@example.com` |
| 5 | `fecha_alta` | bot | Cuándo se recibió el primer dato (`YYYY-MM-DD HH:mm:ss`, hora local). | `2026-10-22 18:30:00` |
| 6 | `fecha_actualizacion` | bot | Última vez que el bot modificó la fila. | `2026-10-22 18:32:10` |

Fila de encabezados exacta (minúsculas): `telefono, estado, nombre_completo, email, fecha_alta, fecha_actualizacion`.
El orden de las columnas puede variar y se pueden agregar columnas propias (ej. `notas`); `telefono` y `estado` son obligatorias.

### Estados

| `estado` | Significado | Qué hace el bot |
| :---: | :--- | :--- |
| `1` | Aprobado | Deriva al flujo de registro de gastos (texto, audio con Whisper, imagen con descripción). |
| `2` | Rechazado | Responde un mensaje estándar de acceso denegado y corta el flujo. No procesa nada. |
| vacío / número nuevo | Desconocido | Inicia (o continúa) el onboarding y avisa que la cuenta requiere aprobación manual del administrador antes de poder registrar gastos. |

Cualquier otro valor en `estado` (`3`, `aprobado`, texto libre…) se trata como vacío: **nunca otorga acceso**.

### Flujo de alta
1. Un número desconocido escribe → el bot le pide nombre y email (solo por texto) y los guarda en una fila nueva con `estado` vacío.
2. Con los datos completos el bot avisa que la cuenta está pendiente; mientras `estado` siga vacío solo responde un recordatorio fijo.
3. El administrador revisa la fila y completa `estado` con `1` o `2`. También puede pre-aprobar a alguien agregando una fila con su `telefono` y `estado` = `1`.
4. El cambio se aplica en hasta `USER_CACHE_TTL_SECONDS` (60 s por defecto): el bot guarda la tabla en memoria para no agotar la cuota de lectura de la API de Sheets.

### Privacidad
La pestaña contiene teléfonos, nombres y emails en claro (necesarios para identificar a quien se aprueba): compartila solo con las personas que administran los accesos.
El bot escribe estos valores sin interpretarlos (`RAW`) y antepone `'` a textos que empiecen con `=`, `+`, `-` o `@`, para evitar inyección de fórmulas / CSV.
