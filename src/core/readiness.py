"""Configuration checks for startup and readiness: name what an operator forgot, never expose values.

No network calls: a readiness probe must stay cheap and must not fail because a provider is throttling.
"""

import os
from src.core.config import Settings

KNOWN_STORAGE_BACKENDS = ("sheets", "postgres")


def _google_credentials_available(settings: Settings) -> bool:
    """Base64 credentials, an existing key file, or the auto-discovered `config/credentials/*.json`."""
    if settings.GCP_SERVICE_ACCOUNT_B64:
        return True
    configured_path = settings.GOOGLE_APPLICATION_CREDENTIALS
    if configured_path:
        return os.path.exists(configured_path)
    directory = "config/credentials"
    try:
        return any(name.endswith(".json") for name in os.listdir(directory))
    except OSError:
        return False


def missing_configuration(settings: Settings) -> list[str]:
    """Names of the settings the bot cannot work without.

    The users tab always lives in Google Sheets, so Google credentials are required whichever backend
    stores the expenses. Optional integrations (Apps Script secret, reports folder) are not listed.
    """
    required = {
        "GROQ_API_KEY": settings.GROQ_API_KEY.get_secret_value(),
        "EVOLUTION_API_KEY": settings.EVOLUTION_API_KEY.get_secret_value(),
        "EVOLUTION_INSTANCE": settings.EVOLUTION_INSTANCE,
        "EVOLUTION_WEBHOOK_SECRET": settings.EVOLUTION_WEBHOOK_SECRET.get_secret_value(),
        "SPREADSHEET_ID": settings.SPREADSHEET_ID,
    }
    missing = [name for name, value in required.items() if not value]

    if settings.STORAGE_BACKEND.lower() not in KNOWN_STORAGE_BACKENDS:
        missing.append("STORAGE_BACKEND")
    if not _google_credentials_available(settings):
        missing.append("GOOGLE_APPLICATION_CREDENTIALS o GCP_SERVICE_ACCOUNT_B64")
    return missing
