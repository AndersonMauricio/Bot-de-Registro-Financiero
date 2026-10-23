"""Shared Google Sheets authentication used by every gspread-backed repository."""

import base64
import json
import os
from functools import lru_cache
import gspread
from google.oauth2.service_account import Credentials
from src.core.config import settings
from src.core.exceptions import StorageError
from src.core.logging import logger

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


@lru_cache(maxsize=1)
def get_gspread_client() -> gspread.Client:
    """Authenticate with GCP using local file or Base64 environment variable (one client per process)."""
    try:
        # 1. Check if Base64 credential string is provided
        if settings.GCP_SERVICE_ACCOUNT_B64:
            raw_json = base64.b64decode(
                settings.GCP_SERVICE_ACCOUNT_B64.get_secret_value()
            ).decode("utf-8")
            creds_dict = json.loads(raw_json)
            credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        # 2. Check if file path is provided
        elif settings.GOOGLE_APPLICATION_CREDENTIALS and os.path.exists(
            settings.GOOGLE_APPLICATION_CREDENTIALS
        ):
            credentials = Credentials.from_service_account_file(
                settings.GOOGLE_APPLICATION_CREDENTIALS, scopes=SCOPES
            )
        # 3. Auto-discover any JSON credentials file in config/credentials/
        elif os.path.exists("config/credentials"):
            candidates = [
                os.path.join("config/credentials", f)
                for f in os.listdir("config/credentials")
                if f.endswith(".json")
            ]
            if candidates:
                logger.info(f"Auto-discovered Google credentials file: {candidates[0]}")
                credentials = Credentials.from_service_account_file(candidates[0], scopes=SCOPES)
            else:
                raise StorageError(
                    "No .json credential file found in config/credentials/ and no GOOGLE_APPLICATION_CREDENTIALS configured."
                )
        else:
            raise StorageError(
                "No valid Google Cloud credentials found. Place a Service Account .json file in config/credentials/ or configure GOOGLE_APPLICATION_CREDENTIALS."
            )

        return gspread.authorize(credentials)
    except Exception as e:
        logger.error(f"Failed to authenticate with Google Sheets: {e}")
        raise StorageError(f"Authentication error with Google Sheets: {e}") from e
