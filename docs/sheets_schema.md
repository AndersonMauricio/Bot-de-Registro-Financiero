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
