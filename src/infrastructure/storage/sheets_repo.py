"""Google Sheets repository implementation using gspread."""

import threading
from typing import Optional
import gspread
from src.core.config import settings
from src.core.exceptions import StorageError
from src.core.logging import logger
from src.domain.models import ExpenseRecord
from src.infrastructure.storage.base import ExpenseRepositoryInterface
from src.infrastructure.storage.sheets_client import get_gspread_client


class GoogleSheetsExpenseRepository(ExpenseRepositoryInterface):
    """Saves expenses into Google Sheets as MVP staging storage."""

    def __init__(self):
        self._client: Optional[gspread.Client] = None
        self._sheet: Optional[gspread.Worksheet] = None
        self._processed_message_ids: set[str] = set()
        # Saves run in worker threads (see ExpenseProcessingService): keep appends one at a time
        self._lock = threading.Lock()

    def _get_client(self) -> gspread.Client:
        """Authenticated gspread client shared with the other Sheets repositories."""
        if not self._client:
            self._client = get_gspread_client()
        return self._client

    def _ensure_headers(self, ws: gspread.Worksheet) -> None:
        """Create header row if worksheet is completely empty."""
        try:
            values = ws.get_all_values()
            if not values:
                headers = [
                    "timestamp",
                    "fecha_gasto",
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
            with self._lock:
                ws = self._get_worksheet()
                row = record.to_sheets_row()
                ws.append_row(row, value_input_option="USER_ENTERED")
                self._processed_message_ids.add(record.message_id)
            logger.info(f"Expense {record.transaction_id} successfully persisted in Google Sheets.")
            return str(record.transaction_id)
        except Exception as e:
            logger.error(f"Failed to append row in Google Sheets: {e}")
            raise StorageError(f"Google Sheets persistence failed: {e}") from e
