"""Google Sheets repository implementation using gspread."""

import threading
from typing import Any, Optional
import gspread
from gspread.utils import DateTimeOption, ValueRenderOption, rowcol_to_a1
from src.core.config import settings
from src.core.exceptions import StorageError
from src.core.logging import logger
from src.domain.models import (
    EXPENSE_DATA_COLUMNS,
    EXPENSE_SHEET_HEADERS,
    OPTIONAL_EXPENSE_SHEET_HEADERS,
    REQUIRED_EXPENSE_SHEET_HEADERS,
    ExpenseRecord,
)
from src.infrastructure.storage.base import ExpenseRepositoryInterface
from src.infrastructure.storage.sheets_client import get_gspread_client

# Identifier columns are written as text so Sheets never reinterprets them (a hash made only of digits
# and a single "e" would be parsed as a number); the leading apostrophe is not stored as part of the value.
TEXT_COLUMNS = ("message_id", "user_phone_hash", "reply_message_id")
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
        missing = [name for name in REQUIRED_EXPENSE_SHEET_HEADERS if name not in columns]
        if missing:
            raise StorageError(
                f"Worksheet '{settings.WORKSHEET_NAME}' is missing the columns {missing}. "
                f"Its header row must contain: {', '.join(EXPENSE_SHEET_HEADERS)}"
            )
        absent_optional = [name for name in OPTIONAL_EXPENSE_SHEET_HEADERS if name not in columns]
        if absent_optional:
            logger.warning(
                f"Worksheet '{settings.WORKSHEET_NAME}' has no {absent_optional} column(s): expenses can only be "
                f"corrected by quoting the user's own message, not the bot's confirmation."
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

    def _is_known(self, message_id: str) -> bool:
        """True when the id or any of its batch rows (`<id>-0`, `<id>-1`, ...) was already stored."""
        if message_id in self._processed_message_ids:
            return True
        prefix = f"{message_id}-"
        return any(stored.startswith(prefix) for stored in self._processed_message_ids)

    def is_message_processed(self, message_id: str) -> bool:
        """Check if message_id was already stored (in this process or in a previous run)."""
        if self._is_known(message_id):
            return True
        try:
            with self._lock:
                ws = self._get_worksheet()
                self._load_processed_ids(ws, self._get_columns(ws))
        except Exception as e:
            # Never block ingestion because of this check: fall back to the in-memory ids
            logger.warning(f"Could not load processed message ids from Google Sheets: {e}")
            return False
        return self._is_known(message_id)

    @staticmethod
    def _row_for(record: ExpenseRecord, columns: dict[str, int]) -> list[Any]:
        """One row aligned with the sheet's own column order."""
        values = record.to_sheets_dict()
        row: list[Any] = [""] * (max(columns.values()) + 1)
        for name, idx in columns.items():
            if name in values:
                value = values[name]
                row[idx] = f"'{value}" if name in TEXT_COLUMNS else value
        return row

    def save_expenses(self, records: list[ExpenseRecord]) -> list[str]:
        """Append all records in a single API call (one request: either every row lands or none)."""
        if not records:
            return []
        try:
            with self._lock:
                ws = self._get_worksheet()
                columns = self._get_columns(ws)
                rows = [self._row_for(record, columns) for record in records]
                if len(rows) == 1:
                    ws.append_row(rows[0], value_input_option="USER_ENTERED")
                else:
                    ws.append_rows(rows, value_input_option="USER_ENTERED")
                self._processed_message_ids.update(record.message_id for record in records)
            logger.info(f"{len(records)} expense(s) successfully persisted in Google Sheets.")
            return [str(record.transaction_id) for record in records]
        except Exception as e:
            logger.error(f"Failed to append rows in Google Sheets: {e}")
            raise StorageError(f"Google Sheets persistence failed: {e}") from e

    def save_expense(self, record: ExpenseRecord) -> str:
        """Append a single record (delegates to the batch writer)."""
        return self.save_expenses([record])[0]

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

    @staticmethod
    def _cell(row: list, index: int) -> str:
        """Text of one cell, without the apostrophe that keeps identifier columns as text."""
        return str(row[index]).strip().lstrip("'") if index < len(row) else ""

    def _read_rows(self, ws: gspread.Worksheet) -> list[list]:
        """All values, with numbers as typed and dates as formatted strings."""
        return ws.get_all_values(
            value_render_option=ValueRenderOption.unformatted,
            date_time_render_option=DateTimeOption.formatted_string,
        )

    def _locate_row(
        self,
        rows: list[list],
        columns: dict[str, int],
        message_id: str,
        user_phone_hash: str,
    ) -> Optional[tuple[int, list]]:
        """1-based row number and raw values of one user's expense cited by `message_id`.

        The cited id may be the user's original message or the bot's confirmation for it, but the row
        must belong to the sender: a quote never reaches another person's data.
        """
        if not (message_id and user_phone_hash):
            return None
        id_idx, hash_idx = columns["message_id"], columns["user_phone_hash"]
        reply_idx = columns.get("reply_message_id")
        for offset, row in enumerate(rows[1:]):
            if self._cell(row, hash_idx) != user_phone_hash:
                continue
            if self._cell(row, id_idx) == message_id:
                return offset + 2, row
            if reply_idx is not None and self._cell(row, reply_idx) == message_id:
                return offset + 2, row
        return None

    def find_expenses_by_message_id(self, message_id: str, user_phone_hash: str) -> list[dict]:
        """All of this user's expenses cited by `message_id` (a batch shares the confirmation id)."""
        if not (message_id and user_phone_hash):
            return []
        try:
            with self._lock:
                ws = self._get_worksheet()
                columns = self._get_columns(ws)
                rows = self._read_rows(ws)
        except StorageError:
            raise
        except Exception as e:
            logger.error(f"Failed to read expenses from Google Sheets: {e}")
            raise StorageError(f"Google Sheets read failed: {e}") from e

        found: list[dict] = []
        id_idx, hash_idx = columns["message_id"], columns["user_phone_hash"]
        reply_idx = columns.get("reply_message_id")
        for row in rows[1:]:
            if self._cell(row, hash_idx) != user_phone_hash:
                continue
            if self._cell(row, id_idx) != message_id and (
                reply_idx is None or self._cell(row, reply_idx) != message_id
            ):
                continue
            stored = {name: (row[idx] if idx < len(row) else "") for name, idx in columns.items()}
            for name in TEXT_COLUMNS:
                if isinstance(stored.get(name), str):
                    stored[name] = stored[name].strip().lstrip("'")
            found.append(stored)
        return found

    def find_expense_by_message_id(self, message_id: str, user_phone_hash: str) -> Optional[dict]:
        """First of this user's own expenses cited by `message_id`."""
        matches = self.find_expenses_by_message_id(message_id, user_phone_hash)
        return matches[0] if matches else None

    def update_expense(self, record: ExpenseRecord) -> bool:
        """Overwrite the data cells of the row of `record.message_id`, only if it belongs to that user."""
        try:
            with self._lock:
                ws = self._get_worksheet()
                columns = self._get_columns(ws)
                located = self._locate_row(
                    self._read_rows(ws), columns, record.message_id, record.user_phone_hash
                )
                if not located:
                    logger.warning(f"Expense {record.message_id} is no longer in the sheet; nothing was updated.")
                    return False
                row_number, _ = located
                values = record.to_sheets_dict()
                updates = [
                    {"range": rowcol_to_a1(row_number, columns[name] + 1), "values": [[values[name]]]}
                    for name in EXPENSE_DATA_COLUMNS
                    if name in columns and name in values
                ]
                if updates:
                    ws.batch_update(updates, raw=False)  # USER_ENTERED, exactly like the original append
            logger.info(f"Expense {record.message_id} updated in Google Sheets.")
            return True
        except Exception as e:
            logger.error(f"Failed to update an expense row in Google Sheets: {e}")
            raise StorageError(f"Google Sheets update failed: {e}") from e

    def set_reply_message_id(self, message_id: str, reply_message_id: str, user_phone_hash: str) -> None:
        """Remember the bot confirmation id of an expense. Best effort: the user was already answered."""
        self.set_reply_message_ids([message_id], reply_message_id, user_phone_hash)

    def set_reply_message_ids(
        self, message_ids: list[str], reply_message_id: str, user_phone_hash: str
    ) -> None:
        """Store the same confirmation id in every row of a batch, with one read and one update."""
        if not (message_ids and reply_message_id):
            return
        try:
            with self._lock:
                ws = self._get_worksheet()
                columns = self._get_columns(ws)
                reply_idx = columns.get("reply_message_id")
                if reply_idx is None:
                    return
                rows = self._read_rows(ws)
                updates = []
                for message_id in message_ids:
                    located = self._locate_row(rows, columns, message_id, user_phone_hash)
                    if located:
                        updates.append(
                            {
                                "range": rowcol_to_a1(located[0], reply_idx + 1),
                                "values": [[f"'{reply_message_id}"]],
                            }
                        )
                    else:
                        logger.warning(f"Expense {message_id} is no longer in the sheet; its reply id was not stored.")
                if updates:
                    ws.batch_update(updates, raw=False)
        except Exception as e:
            # Losing the link only degrades later corrections: never fail the answer that is already sent
            logger.warning(f"Could not store the reply id of {len(message_ids)} expense(s): {e}")
