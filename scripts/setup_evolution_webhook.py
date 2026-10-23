"""CLI to point the Evolution API instance webhook at this service (MESSAGES_UPSERT events only)."""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath("."))

from src.core.config import settings
from src.core.exceptions import WhatsAppClientError
from src.infrastructure.whatsapp.client import EvolutionAPIClient


async def main(url: str) -> int:
    print("=" * 60)
    print("🔌 CONFIGURANDO EL WEBHOOK DE EVOLUTION API...")
    print(f"🌐 Evolution API: {settings.EVOLUTION_API_URL}")
    print(f"📱 Instancia:     {settings.EVOLUTION_INSTANCE or '(sin configurar)'}")
    print(f"↩️  Webhook URL:   {url}")
    print("=" * 60)

    secret = settings.EVOLUTION_WEBHOOK_SECRET.get_secret_value()
    if not secret:
        print("\n❌ Falta EVOLUTION_WEBHOOK_SECRET en el .env. Generá uno con:")
        print('   python -c "import secrets; print(secrets.token_urlsafe(32))"')
        return 1

    try:
        result = await EvolutionAPIClient().set_webhook(url=url, secret=secret)
    except WhatsAppClientError as e:
        print(f"\n❌ ERROR: {e}")
        print("\n🛠️ Revisá EVOLUTION_API_URL, EVOLUTION_API_KEY y EVOLUTION_INSTANCE, y que la instancia exista.")
        return 1

    # Evolution echoes the stored headers (our secret): print only the non-sensitive fields
    webhook = result.get("webhook", result)
    print("\n✅ Webhook configurado:")
    for field in ("enabled", "url", "events", "byEvents", "webhookByEvents", "base64", "webhookBase64"):
        if field in webhook:
            print(f"   {field}: {webhook[field]}")
    print("\n👉 Evolution enviará el secreto en el header 'x-webhook-secret'; el bot lo valida en cada llamada.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        required=True,
        help="URL pública (o interna de Docker) del endpoint, ej. https://<tu-dominio>/api/v1/webhooks/evolution",
    )
    args = parser.parse_args()
    if not args.url.startswith(("http://", "https://")):
        parser.error("--url debe empezar con http:// o https://")
    sys.exit(asyncio.run(main(args.url)))
