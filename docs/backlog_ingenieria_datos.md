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

### Decisiones de implementación

- **Punto 1 – "eliminar solo ese registro":** el bot guarda la lista completa en una sola lectura, así que borrar un registro lo haría pasar por "desconocido". En su lugar marca ese número como vencido: su próxima consulta relee la hoja **una vez** (y de paso refresca al resto). Si esa relectura falla, **cierra el acceso** en vez de usar el dato viejo. El secreto es distinto al de Evolution.
- **Punto 3:** los enums aceptan variaciones de mayúsculas y tildes (`ALIMENTACION` → `Alimentación`) pero rechazan cualquier valor fuera de la taxonomía; los valores en inglés anteriores (`CASH`, `CREDIT_CARD`…) ya no son válidos. Un test verifica que enums, prompt y `data_contract.json` listen lo mismo.
- **Punto 5 – intercepción:** se hace en el pipeline de gastos **después** de obtener el texto (o la transcripción), para que *"quiero mi resumen"* dicho por audio no se registre como gasto. La detección es determinista (sin LLM): un pedido de reporte nunca lleva un monto.
- **Punto 6:** `Fecha_Registro` se genera cuando el bot ve el número por primera vez, y desaparece `fecha_actualizacion`. Las pestañas con los nombres de la versión anterior (`nombre_completo`, `fecha_alta`) siguen funcionando. `raw_expenses` se escribe por nombre de columna y, si falta alguna de las 10, el bot se niega a guardar en vez de desalinear datos. La idempotencia por `message_id` ahora sobrevive a reinicios.

### Riesgos a tener presentes

- **El hash no es un secreto fuerte:** SHA-256 sin sal de un número de teléfono se revierte por fuerza bruta (hay pocos números posibles). Sirve para no exponer el teléfono a simple vista y para filtrar, pero no protege ante quien tenga acceso a la hoja y quiera descubrir un número. Una mejora posible es `HMAC-SHA256` con una clave del servidor.
- **El email no está verificado:** el reporte se comparte con la dirección que la persona escribió en el onboarding. Quien apruebe usuarios debería confirmar que pertenece a esa persona.
- **Cada reporte es un archivo nuevo en Drive** de la cuenta de servicio; se limita con un enfriamiento por usuario (`REPORT_COOLDOWN_SECONDS`), pero no se borran los viejos.
- **Registro al primer contacto:** cualquier número que escriba queda registrado en `usuarios` (una escritura en la hoja). No hay límite por número desconocido.

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
