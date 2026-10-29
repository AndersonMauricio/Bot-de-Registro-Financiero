# 🐳 Instructivo: levantar el proyecto punta a punta con Docker

Guía paso a paso para tener el bot funcionando de verdad: **Evolution API** (el WhatsApp del bot) y el **servicio FastAPI**, ambos en contenedores. Si preferís correr el bot sin Docker, usá el [instructivo local](instructivo_local.md).

```mermaid
flowchart LR
    PH["Segundo teléfono"] --> WA["WhatsApp"]
    WA --> EVO["Evolution API (Docker, puerto 8080)"]
    EVO -->|"webhook con x-webhook-secret"| BOT["Bot FastAPI (Docker, puerto 8000)"]
    BOT -->|"respuesta y escribiendo..."| EVO
    BOT --> GROQ["Groq (Whisper y LLM)"]
    BOT --> GS["Google Sheets (usuarios y raw_expenses)"]
```

> **¿Dónde se guardan mis datos? En Google Sheets.** Los gastos (`raw_expenses`) y los usuarios (`usuarios`) viven solo en tu spreadsheet; el bot **no necesita PostgreSQL**. Los PostgreSQL que aparecen en esta guía son otra cosa:
> - **El de Evolution** (dentro de su compose): es la base interna de Evolution API, que la exige para guardar la sesión de WhatsApp y los mensajes que recibe. No se puede omitir, pero vive en su propio contenedor y no hay nada que administrar. Guarda una copia de los mensajes, aparte de tu hoja.
> - **El del `docker-compose.yml` del bot** (`bot_wpp_postgres`): solo se usa si algún día configurás `STORAGE_BACKEND=postgres`. Con el valor por defecto (`sheets`) queda levantado sin usarse.

## Requisitos

- **Docker Desktop** corriendo (incluye `docker compose`).
- **Python 3** en tu máquina, solo para ejecutar un script de una línea (el que configura el webhook) y generar claves. El proyecto trae un `venv/` con todo lo necesario.
- Una **API key de Groq** y la **cuenta de servicio de Google** (el `.json` en `config/credentials/`).
- **Un segundo teléfono**: el bot "es" el WhatsApp que vinculás por QR, y los mensajes que te mandás desde ese mismo número se ignoran a propósito. Conviene vincular un número dedicado al bot: Evolution usa una conexión no oficial de WhatsApp.

## ✅ Qué tenés que completar antes de empezar

1. **Rotá la `GROQ_API_KEY`** si alguna vez estuvo en un commit (aunque hayas limpiado el historial, dala por comprometida) y pegá la nueva en tu `.env`.
2. **Completá tu `.env`.** Si ya tenés uno, agregale estas variables (no hagas `cp .env.example .env`, que lo pisa):

   | Variable | Valor para Docker | De dónde sale |
   | :--- | :--- | :--- |
   | `EVOLUTION_API_URL` | `http://host.docker.internal:8080` | Es cómo el contenedor del bot llega a Evolution a través de tu máquina. |
   | `EVOLUTION_API_KEY` | la `AUTHENTICATION_API_KEY` del Paso 1 | La definís vos al crear el `.env` de Evolution. |
   | `EVOLUTION_INSTANCE` | `bot-wpp` | El nombre que le des a la instancia en el Paso 2. |
   | `EVOLUTION_WEBHOOK_SECRET` | una clave larga aleatoria | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |

   Opcionales: `SHEETS_WEBHOOK_SECRET` (aprobaciones en tiempo real, requiere una URL pública: ver [apps_script_usuarios.md](apps_script_usuarios.md)), `REPORTS_FOLDER_ID` y `REPORT_COOLDOWN_SECONDS`.

   El resto (`GROQ_API_KEY`, `GOOGLE_SHEETS_SHEET_ID` o `SPREADSHEET_ID`, `GOOGLE_SHEETS_TAB_NAME` o `WORKSHEET_NAME`, `POSTGRES_*`) ya debería estar. Las variables viejas `N8N_*`, `WEBHOOK_URL` y `WHATSAPP_*` no se usan más: podés borrarlas (y revocar el token de Meta).
3. **Google Sheets.**
   - Compartí la hoja (rol **Editor**) con el `client_email` de la cuenta de servicio: `grep client_email config/credentials/*.json`.
   - Creá la pestaña **`usuarios`** con los encabezados de [sheets_schema.md](sheets_schema.md#pestaña-usuarios-control-de-acceso). En macOS podés copiarlos y pegarlos en `A1`:
     ```bash
     printf 'Telefono\tNombre\tEmail\tEstado\tFecha_Registro\n' | pbcopy
     ```
     Si la pestaña no existe, el bot no puede verificar a nadie y rechaza todos los mensajes.
   - La pestaña de gastos (`raw_expenses` por defecto) tiene que existir. Si está vacía, el bot crea sus encabezados; si ya tiene otra estructura, **reemplazala** por la de [sheets_schema.md](sheets_schema.md#pestaña-raw_expenses-registro-de-gastos), porque si falta alguna de las 10 columnas obligatorias el bot se niega a guardar. La 11ª (`reply_message_id`) es opcional: sin ella no se pueden corregir gastos citando la confirmación del bot. En macOS:
     ```bash
     printf 'timestamp\tmessage_id\tuser_phone_hash\tfecha_gasto\tmonto\tmoneda\tcategoria\tsubcategoria\tmedio_de_pago\tdescripcion\treply_message_id\n' | pbcopy
     ```
   - Para los **reportes** habilitá la **Google Drive API** en el proyecto de GCP de la cuenta de servicio (y, opcionalmente, `REPORTS_FOLDER_ID` en el `.env`).

## 🚀 Paso a paso

### 1. Levantar Evolution API

Creá una carpeta **fuera del repo** (para no commitear sus claves), por ejemplo `~/evolution`, con este `docker-compose.yml`:

```yaml
services:
  evolution-api:
    image: evoapicloud/evolution-api:v2.3.7   # ver nota sobre versiones
    restart: unless-stopped
    depends_on:
      evolution-postgres: {condition: service_healthy}
      evolution-redis: {condition: service_started}
    ports: ["127.0.0.1:8080:8080"]
    environment:
      SERVER_URL: http://localhost:8080
      AUTHENTICATION_API_KEY: ${AUTHENTICATION_API_KEY}
      DATABASE_PROVIDER: postgresql
      DATABASE_CONNECTION_URI: postgresql://evolution:${EVOLUTION_DB_PASSWORD}@evolution-postgres:5432/evolution?schema=public
      DATABASE_SAVE_DATA_INSTANCE: "true"
      DATABASE_SAVE_DATA_NEW_MESSAGE: "true"   # necesario para el fallback de descarga de audio
      DATABASE_SAVE_MESSAGE_UPDATE: "true"
      DATABASE_SAVE_DATA_CONTACTS: "true"
      DATABASE_SAVE_DATA_CHATS: "true"
      CACHE_REDIS_ENABLED: "true"
      CACHE_REDIS_URI: redis://evolution-redis:6379/6
      CACHE_REDIS_PREFIX_KEY: evolution
      CACHE_REDIS_SAVE_INSTANCES: "false"
      CACHE_LOCAL_ENABLED: "false"
    volumes: [evolution_instances:/evolution/instances]
  evolution-postgres:
    image: postgres:15-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: evolution
      POSTGRES_USER: evolution
      POSTGRES_PASSWORD: ${EVOLUTION_DB_PASSWORD}
    volumes: [evolution_pg:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U evolution -d evolution"]
      interval: 5s
      timeout: 5s
      retries: 10
  evolution-redis:
    image: redis:7-alpine
    restart: unless-stopped
    volumes: [evolution_redis:/data]
volumes: {evolution_instances: {}, evolution_pg: {}, evolution_redis: {}}
```

Generá sus claves y levantalo:

```bash
cd ~/evolution
printf 'AUTHENTICATION_API_KEY=%s\nEVOLUTION_DB_PASSWORD=%s\n' \
  "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  "$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')" > .env
docker compose up -d --wait
curl -s localhost:8080/     # {"status":200,"message":"Welcome to the Evolution API, it is working!","version":"2.3.7",...}
```

Copiá el valor de `AUTHENTICATION_API_KEY` de ese `.env` a `EVOLUTION_API_KEY` en el `.env` del bot.

> **Versiones:** usá `v2.3.7` (el último estable). `latest` apunta hoy a un release candidate 2.4.x y, según la documentación oficial de Evolution, desde la 2.4.0 cada instancia debe activarse contra su servidor de licencias antes de responder (no lo probé).

### 2. Vincular el WhatsApp del bot

Desde la misma carpeta (`~/evolution`):

```bash
KEY=$(grep '^AUTHENTICATION_API_KEY=' .env | cut -d= -f2)
curl -s -X POST localhost:8080/instance/create -H "apikey: $KEY" -H 'Content-Type: application/json' \
  -d '{"instanceName":"bot-wpp","integration":"WHATSAPP-BAILEYS","qrcode":true}' >/dev/null
curl -s localhost:8080/instance/connect/bot-wpp -H "apikey: $KEY" \
  | python3 -c "import sys,json,base64; open('qr.png','wb').write(base64.b64decode(json.load(sys.stdin)['base64'].split(',',1)[1]))" && open qr.png
```

Escaneá el QR desde el teléfono del bot: **WhatsApp → Dispositivos vinculados → Vincular un dispositivo**. El QR vence rápido: si pasa, repetí solo el segundo `curl`. Confirmá la conexión (tiene que decir `"state":"open"`):

```bash
curl -s localhost:8080/instance/connectionState/bot-wpp -H "apikey: $KEY"
```

Alternativa visual: el Manager de Evolution en `http://localhost:8080/manager` (se entra con la URL del servidor y la API key global).

### 3. Levantar el bot

Con el `.env` del proyecto ya completo, en la carpeta del repo:

```bash
docker compose up -d --build --wait
curl -s localhost:8000/api/v1/health     # {"status":"ok",...}
```

Levanta el servicio (`bot_wpp_app`, puerto 8000) y un PostgreSQL (`bot_wpp_postgres`, puerto 5432) que solo se usa si configurás `STORAGE_BACKEND=postgres`. El contenedor lee `config/credentials/` en modo solo lectura.

### 4. Conectar Evolution con el bot

Esto le dice a la instancia que envíe cada mensaje nuevo (`MESSAGES_UPSERT`, con el audio inline) al bot, con tu secreto en el header `x-webhook-secret`. Desde la carpeta del repo:

```bash
EVOLUTION_API_URL=http://localhost:8080 venv/bin/python scripts/setup_evolution_webhook.py \
  --url http://host.docker.internal:8000/api/v1/webhooks/evolution
```

Dos detalles importantes:
- `EVOLUTION_API_URL=http://localhost:8080` es solo para este comando: **desde tu terminal** `host.docker.internal` no resuelve (solo existe dentro de los contenedores).
- En `--url` sí va `host.docker.internal`: es cómo el contenedor de Evolution llega al bot a través de tu máquina.

Tiene que terminar con `✅ Webhook configurado` y `events: ['MESSAGES_UPSERT']`.

### 5. Probar de punta a punta

Dejá los logs abiertos con `docker compose logs -f app` y, **desde el segundo teléfono**, escribile al número del bot:

1. **`hola`** → el bot te pide nombre y apellido, después el email, y cierra avisando que la cuenta **requiere aprobación manual del administrador**.
2. En la pestaña `usuarios` aparece tu fila con `estado` vacío. Cargá **`1`** (aprobado).
3. Esperá hasta **60 segundos** (el bot cachea la lista de usuarios) y mandá *"Gasté 15 USD en almuerzo con débito"* → responde "✅ Gasto Registrado con Éxito" y aparece una fila en `raw_expenses`.
4. **Corregí ese gasto**: respondé (citando) el mensaje de confirmación del bot con *"me equivoqué, eran 150 USD"* → responde "✏️ Registro modificado" y la fila se actualiza **en el lugar** (no aparece una fila nueva). Requiere la columna `reply_message_id` en la hoja.
5. Mandá una **nota de voz** con un gasto: se transcribe con Whisper y se registra igual.
6. Cambiá `estado` a **`2`** (rechazado), esperá la caché y escribí de nuevo → responde "⛔ Acceso denegado".

7. Pedí tu **reporte** escribiendo *"quiero mi resumen"* (o por audio): el bot responde con el link a un Google Spreadsheet con **solo tus gastos**, compartido en solo lectura con el email que dejaste en el onboarding. Hay un enfriamiento de 5 minutos entre reportes.

Mientras Groq procesa, el chat muestra "escribiendo…".

## 🔧 Operación diaria

| Qué querés hacer | Comando |
| :--- | :--- |
| Ver logs del bot | `docker compose logs -f app` (carpeta del repo) |
| Ver logs de Evolution | `docker compose logs -f evolution-api` (en `~/evolution`) |
| Cambiaste el `.env` del bot | `docker compose up -d --force-recreate app` (un `restart` **no** relee el `.env`) |
| Reconstruir tras cambiar código | `docker compose up -d --build app` |
| Apagar | `docker compose down` en cada carpeta |
| Borrar también los datos | `docker compose down -v`. En Evolution esto elimina la sesión de WhatsApp: hay que volver a escanear el QR. |

## 🩺 Si algo falla

| Síntoma | Causa probable | Qué hacer |
| :--- | :--- | :--- |
| El webhook responde **503** (log: `EVOLUTION_WEBHOOK_SECRET is not configured`) | Falta la variable en el `.env`, o no recreaste el contenedor | Completala y corré `docker compose up -d --force-recreate app` |
| **401** `Invalid webhook secret` | El secreto del `.env` no coincide con el guardado en Evolution | Repetí el Paso 4 |
| El bot procesa pero no contesta (log: `Failed to send WhatsApp message`) | `EVOLUTION_API_URL` con `localhost` (dentro del contenedor apunta al propio contenedor), o `EVOLUTION_API_KEY` / `EVOLUTION_INSTANCE` mal | Usá `http://host.docker.internal:8080` y revisá las otras dos |
| Responde "No pudimos verificar tu acceso" | Falta la pestaña `usuarios`, los encabezados están mal, o la hoja no está compartida con la cuenta de servicio | Mirá el log del bot (`Users worksheet ... not found`) |
| No llega ningún webhook | La instancia no está `open`, o el webhook no quedó configurado | `curl localhost:8080/instance/connectionState/bot-wpp -H "apikey: $KEY"` y `curl localhost:8080/webhook/find/bot-wpp -H "apikey: $KEY"` |
| No responde a tus mensajes | Le estás escribiendo desde el mismo número vinculado | Usá otro teléfono |
| `port is already allocated` | 8000, 8080 o 5432 ocupados | Liberalos, o cambiá el puerto del lado izquierdo en el compose |
| Falla un audio de iPhone | Bug conocido de Evolution al descargar medios de iOS | Probá con texto o con un audio desde Android |

## 🔬 Qué está verificado

Probado en macOS con Docker Desktop 29 y Evolution API 2.3.7: build y arranque del bot con su healthcheck, lectura de las credenciales de Google dentro del contenedor (usuario no-root), generación del QR, configuración del webhook con el script del repo, entrega de eventos de Evolution con el header de secreto, y la red bot ↔ Evolution a través de `host.docker.internal` en ambos sentidos.

No se puede automatizar y queda para tu primera prueba: el escaneo del QR y los mensajes reales de WhatsApp (texto y audio), tu Google Sheet real y las llamadas a Groq.
