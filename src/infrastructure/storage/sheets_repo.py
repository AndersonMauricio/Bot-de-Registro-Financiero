"""Google Sheets repository implementation using gspread."""

import threading
from typing import Any, Optional
import gspread
from gspread.utils import DateTimeOption, ValueRenderOption
from src.core.config import settings
from src.core.exceptions import StorageError
from src.core.logging import logger
from src.domain.models import EXPENSE_SHEET_HEADERS, ExpenseRecord
from src.infrastructure.storage.base import ExpenseRepositoryInterface
from src.infrastructure.storage.sheets_client import get_gspread_client

# Identifier columns are written as text so Sheets never reinterprets them (a hash made only of digits
# and a single "e" would be parsed as a number); the leading apostrophe is not stored as part of the value.
TEXT_COLUMNS = ("message_id", "user_phone_hash")
# Columns a user is allowed to see in their own report (never the message id or the phone hash)
REPORT_COLUMNS = (
    "fecha_gasto",
    "monto",
    "moneda",
    "categoria",
    "subcategoria",
    "medio_de_pago",
    "descripcion",
)


class GoogleSheetsExpenseRepository(ExpenseRepositoryInterface):
    """Saves expenses into Google Sheets as MVP staging storage.

    Rows are written by header name, so the order of the columns in the sheet does not matter,
    but every column of EXPENSE_SHEET_HEADERS must exist (otherwise saving fails loudly instead of misaligning data).
    """

    def __init__(self, worksheet: Optional[gspread.Worksheet] = None):
        self._client: Optional[gspread.Client] = None
        self._sheet: Optional[gspread.Worksheet] = worksheet
        self._processed_message_ids: set[str] = set()
        self._ids_loaded = False
        self._columns: Optional[dict[str, int]] = None  # validated header -> 0-based column index
        # Saves run in worker threads (see ExpenseProcessingService): keep access one at a time
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
                ws.append_row(EXPENSE_SHEET_HEADERS, value_input_option="USER_ENTERED")
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

    def _get_columns(self, ws: gspread.Worksheet) -> dict[str, int]:
        """Header -> column index, verified once against the expected schema."""
        if self._columns is not None:
            return self._columns
        try:
            header = ws.row_values(1)
        except Exception as e:
            raise StorageError(f"Could not read the header of worksheet '{settings.WORKSHEET_NAME}': {e}") from e
        columns = {str(name).strip().lower(): idx for idx, name in enumerate(header) if str(name).strip()}
        missing = [name for name in EXPENSE_SHEET_HEADERS if name not in columns]
        if missing:
            raise StorageError(
                f"Worksheet '{settings.WORKSHEET_NAME}' is missing the columns {missing}. "
                f"Its header row must contain: {', '.join(EXPENSE_SHEET_HEADERS)}"
            )
        self._columns = columns
        return columns

    def _load_processed_ids(self, ws: gspread.Worksheet, columns: dict[str, int]) -> None:
        """Remember the message ids already stored in the sheet, so idempotency survives restarts."""
        if self._ids_loaded:
            return
        ids = ws.col_values(columns["message_id"] + 1)
        self._processed_message_ids.update(str(v).strip() for v in ids[1:] if v not in (None, ""))
        self._ids_loaded = True
        logger.info(f"Loaded {len(self._processed_message_ids)} processed message ids from the expenses sheet.")

    def is_message_processed(self, message_id: str) -> bool:
        """Check if message_id was already stored (in this process or in a previous run)."""
        if message_id in self._processed_message_ids:
            return True
        try:
            with self._lock:
                ws = self._get_worksheet()
                self._load_processed_ids(ws, self._get_columns(ws))
        except Exception as e:
            # Never block ingestion because of this check: fall back to the in-memory ids
            logger.warning(f"Could not load processed message ids from Google Sheets: {e}")
            return False
        return message_id in self._processed_message_ids

    def save_expense(self, record: ExpenseRecord) -> str:
        """Append record as a new row in Google Sheets."""
        try:
            with self._lock:
                ws = self._get_worksheet()
                columns = self._get_columns(ws)
                values = record.to_sheets_dict()
                row: list[Any] = [""] * (max(columns.values()) + 1)
                for name, idx in columns.items():
                    if name in values:
                        value = values[name]
                        row[idx] = f"'{value}" if name in TEXT_COLUMNS else value
                ws.append_row(row, value_input_option="USER_ENTERED")
                self._processed_message_ids.add(record.message_id)
            logger.info(f"Expense {record.transaction_id} successfully persisted in Google Sheets.")
            return str(record.transaction_id)
        except Exception as e:
            logger.error(f"Failed to append row in Google Sheets: {e}")
            raise StorageError(f"Google Sheets persistence failed: {e}") from e

    def get_expenses_by_user(self, user_phone_hash: str) -> list[dict]:
        """Rows that belong to one user (strict match on user_phone_hash), limited to REPORT_COLUMNS."""
        if not user_phone_hash:
            return []
        try:
            with self._lock:
                ws = self._get_worksheet()
                columns = self._get_columns(ws)
                values = ws.get_all_values(
                    value_render_option=ValueRenderOption.unformatted,
                    date_time_render_option=DateTimeOption.formatted_string,
                )
        except StorageError:
            raise
        except Exception as e:
            logger.error(f"Failed to read expenses from Google Sheets: {e}")
            raise StorageError(f"Google Sheets read failed: {e}") from e

        hash_idx = columns["user_phone_hash"]
        rows = []
        for row in values[1:]:
            if hash_idx < len(row) and str(row[hash_idx]).strip().lstrip("'") == user_phone_hash:
                rows.append({name: row[columns[name]] if columns[name] < len(row) else "" for name in REPORT_COLUMNS})
        return rows
