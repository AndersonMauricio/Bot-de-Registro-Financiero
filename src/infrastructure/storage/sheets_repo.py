"""Google Sheets repository implementation using gspread."""

import base64
import json
import os
from typing import Optional
import gspread
from google.oauth2.service_account import Credentials
from src.core.config import settings
from src.core.exceptions import StorageError
from src.core.logging import logger
from src.domain.models import ExpenseRecord
from src.infrastructure.storage.base import ExpenseRepositoryInterface

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class GoogleSheetsExpenseRepository(ExpenseRepositoryInterface):
    """Saves expenses into Google Sheets as MVP staging storage."""

    def __init__(self):
        self._client: Optional[gspread.Client] = None
        self._sheet: Optional[gspread.Worksheet] = None
        self._processed_message_ids: set[str] = set()

    def _get_client(self) -> gspread.Client:
        """Authenticate with GCP using local file or Base64 environment variable."""
        if self._client:
            return self._client

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

            self._client = gspread.authorize(credentials)
            return self._client
        except Exception as e:
            logger.error(f"Failed to authenticate with Google Sheets: {e}")
            raise StorageError(f"Authentication error with Google Sheets: {e}") from e

    def _ensure_headers(self, ws: gspread.Worksheet) -> None:
        """Create header row if worksheet is completely empty."""
        try:
            values = ws.get_all_values()
            if not values:
                headers = [
                    "timestamp",
                    "monto",
                    "moneda",
                    "categoria",
                    "descripcion",
                ]
                ws.append_row(headers, value_input_option="USER_ENTERED")
                logger.info(f"Initialized headers in empty worksheet '{settings.WORKSHEET_NAME}'.")
        except Exception as e:
            logger.warning(f"Could not verify or initialize headers: {e}")

    def _get_worksheet(self) -> gspread.Worksheet:
        """Obtain target worksheet reference."""
        if self._sheet:
            return self._sheet

        client = self._get_client()
        try:
            spreadsheet = client.open_by_key(settings.SPREADSHEET_ID)
            self._sheet = spreadsheet.worksheet(settings.WORKSHEET_NAME)
            self._ensure_headers(self._sheet)
            return self._sheet
        except Exception as e:
            logger.error(f"Error accessing worksheet '{settings.WORKSHEET_NAME}' in sheet '{settings.SPREADSHEET_ID}': {e}")
            raise StorageError(f"Failed to access Google Sheets worksheet: {e}") from e

    def is_message_processed(self, message_id: str) -> bool:
        """Check if message_id exists in cache."""
        return message_id in self._processed_message_ids

    def save_expense(self, record: ExpenseRecord) -> str:
        """Append record as a new row in Google Sheets."""
        try:
            ws = self._get_worksheet()
            row = record.to_sheets_row()
            ws.append_row(row, value_input_option="USER_ENTERED")
            self._processed_message_ids.add(record.message_id)
            logger.info(f"Expense {record.transaction_id} successfully persisted in Google Sheets.")
            return str(record.transaction_id)
        except Exception as e:
            logger.error(f"Failed to append row in Google Sheets: {e}")
            raise StorageError(f"Google Sheets persistence failed: {e}") from e
