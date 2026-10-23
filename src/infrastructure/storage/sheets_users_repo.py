"""Google Sheets users repository: access states (1 approved / 2 rejected / empty unknown) with an in-memory cache."""

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo
import gspread
from gspread.utils import ValueInputOption, ValueRenderOption, rowcol_to_a1
from src.core.config import settings
from src.core.exceptions import StorageError
from src.core.logging import logger
from src.core.phone import mask_phone, normalize_phone, phone_key
from src.domain.enums import UserStatusEnum
from src.domain.models import OnboardingData, UserRecord
from src.infrastructure.cache.ttl_cache import TTLCache
from src.infrastructure.storage.base import UserRepositoryInterface
from src.infrastructure.storage.sheets_client import get_gspread_client

HEADERS = ["telefono", "estado", "nombre_completo", "email", "fecha_alta", "fecha_actualizacion"]
REQUIRED_HEADERS = ("telefono", "estado")

_SNAPSHOT_KEY = "users"
# If a refresh fails, keep serving the last known users for up to this long (then fail closed)
STALE_GRACE_SECONDS = 900.0
# After a failed refresh, do not hit the Sheets API again before this many seconds
RETRY_BACKOFF_SECONDS = 15.0


@dataclass
class _Table:
    """Parsed users worksheet."""
    columns: dict[str, int]                   # header name -> 0-based column index
    rows: dict[str, tuple[int, UserRecord]]   # phone key -> (1-based sheet row, record)


def parse_status(raw: Any) -> Optional[UserStatusEnum]:
    """Strict mapping of the `estado` cell: only 1 and 2 are valid; empty or anything else is unknown."""
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    text = "" if raw is None else str(raw).strip()
    if not text:
        return None
    if text in ("1", "2"):
        return UserStatusEnum(int(text))
    logger.warning(f"Unrecognized 'estado' value '{text[:20]}' in users sheet; treating the user as unknown.")
    return None


def _stricter(a: Optional[UserStatusEnum], b: Optional[UserStatusEnum]) -> Optional[UserStatusEnum]:
    """For duplicated rows: an explicit rejection wins, then an explicit approval, then unknown."""
    if UserStatusEnum.REJECTED in (a, b):
        return UserStatusEnum.REJECTED
    return a or b


def _local_timestamp() -> str:
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        tz = timezone(timedelta(hours=-3))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


class GoogleSheetsUserRepository(UserRepositoryInterface):
    """Reads user states from a Google Sheets tab, served from memory for `USER_CACHE_TTL_SECONDS`.

    One read of the whole tab refreshes every user, so Sheets read usage stays at about one request per TTL
    no matter how many messages (or unknown numbers) arrive. Bot-initiated writes update the cache directly.
    The approval status (`estado`) is only ever edited by a person in the sheet.
    """

    def __init__(
        self,
        worksheet: Optional[gspread.Worksheet] = None,
        cache: Optional[TTLCache] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._sheet = worksheet
        self._clock = clock
        self._cache: TTLCache = cache or TTLCache(settings.USER_CACHE_TTL_SECONDS, clock=clock)
        self._lock = threading.Lock()
        self._retry_not_before = 0.0

    # ------------------------------------------------------------------ reads
    def get_user(self, phone: str) -> Optional[UserRecord]:
        return self._load_users().get(phone_key(phone))

    def _load_users(self) -> dict[str, UserRecord]:
        """Return the cached users table, refreshing it from the sheet when it expired."""
        with self._lock:  # single-flight: concurrent misses trigger one read
            users = self._cache.get(_SNAPSHOT_KEY)
            if users is not None:
                return users

            stale = self._cache.get(_SNAPSHOT_KEY, max_stale=STALE_GRACE_SECONDS)
            if self._clock() < self._retry_not_before:
                if stale is not None:
                    return stale
                raise StorageError("Users sheet temporarily unavailable; retrying shortly.")

            try:
                table = self._read_table()
            except StorageError as e:
                self._retry_not_before = self._clock() + RETRY_BACKOFF_SECONDS
                if stale is not None:
                    logger.warning(f"Users sheet refresh failed ({e}); serving last known users.")
                    return stale
                raise

            self._retry_not_before = 0.0
            users = {key: record for key, (_, record) in table.rows.items()}
            self._cache.set(_SNAPSHOT_KEY, users)
            logger.info(f"Users sheet refreshed: {len(users)} registered users cached.")
            return users

    def _get_worksheet(self) -> gspread.Worksheet:
        if self._sheet:
            return self._sheet
        try:
            spreadsheet = get_gspread_client().open_by_key(settings.SPREADSHEET_ID)
            self._sheet = spreadsheet.worksheet(settings.USERS_WORKSHEET_NAME)
            return self._sheet
        except gspread.WorksheetNotFound as e:
            raise StorageError(
                f"Users worksheet '{settings.USERS_WORKSHEET_NAME}' not found. "
                f"Create it with these headers: {', '.join(HEADERS)}"
            ) from e
        except Exception as e:
            logger.error(f"Error accessing users worksheet '{settings.USERS_WORKSHEET_NAME}': {e}")
            raise StorageError(f"Failed to access users worksheet: {e}") from e

    def _read_table(self) -> _Table:
        """Read and parse the whole users tab (a single Sheets read request)."""
        ws = self._get_worksheet()
        try:
            values = ws.get_all_values(value_render_option=ValueRenderOption.unformatted)
            if not values:
                ws.append_row(HEADERS, value_input_option=ValueInputOption.raw)
                logger.info(f"Initialized headers in empty worksheet '{settings.USERS_WORKSHEET_NAME}'.")
                values = [HEADERS]
        except Exception as e:
            logger.error(f"Error reading users worksheet: {e}")
            raise StorageError(f"Failed to read users worksheet: {e}") from e
        return self._parse_table(values)

    @staticmethod
    def _cell(row: list, columns: dict[str, int], name: str) -> Any:
        idx = columns.get(name)
        return row[idx] if idx is not None and idx < len(row) else ""

    def _parse_table(self, values: list[list]) -> _Table:
        columns = {str(h).strip().lower(): i for i, h in enumerate(values[0]) if str(h).strip()}
        missing = [h for h in REQUIRED_HEADERS if h not in columns]
        if missing:
            raise StorageError(
                f"Users worksheet is missing required columns {missing}. Expected headers: {', '.join(HEADERS)}"
            )

        rows: dict[str, tuple[int, UserRecord]] = {}
        for row_number, row in enumerate(values[1:], start=2):
            phone_raw = self._cell(row, columns, "telefono")
            key = phone_key(phone_raw)
            if not key:
                continue
            record = UserRecord(
                phone=normalize_phone(phone_raw),
                status=parse_status(self._cell(row, columns, "estado")),
                full_name=str(self._cell(row, columns, "nombre_completo")).strip() or None,
                email=str(self._cell(row, columns, "email")).strip() or None,
            )
            if key in rows:
                logger.warning(f"Duplicated phone {mask_phone(key)} in users sheet (rows {rows[key][0]} and {row_number}).")
                first_row, first = rows[key]
                record = first.model_copy(update={"status": _stricter(first.status, record.status)})
                row_number = first_row
            rows[key] = (row_number, record)
        return _Table(columns=columns, rows=rows)

    # ----------------------------------------------------------------- writes
    def save_onboarding(self, phone: str, data: OnboardingData) -> UserRecord:
        key = phone_key(phone)
        with self._lock:
            ws = self._get_worksheet()
            # Fresh read: cached row numbers could be stale if someone sorted or edited the sheet.
            table = self._read_table()
            now = _local_timestamp()
            existing = table.rows.get(key)

            absent = [h for h in HEADERS if h not in table.columns]
            if absent:
                logger.warning(f"Users sheet has no {absent} column(s); those values will not be stored.")

            try:
                if existing is None:
                    record = UserRecord(
                        phone=normalize_phone(phone),
                        status=None,
                        full_name=data.full_name,
                        email=data.email,
                    )
                    ws.append_row(
                        self._build_row(table.columns, {
                            "telefono": record.phone,
                            "estado": "",
                            "nombre_completo": record.full_name or "",
                            "email": record.email or "",
                            "fecha_alta": now,
                            "fecha_actualizacion": now,
                        }),
                        value_input_option=ValueInputOption.raw,  # RAW: user text is never parsed as a formula
                    )
                else:
                    row_number, current = existing
                    record = current.model_copy(update={
                        "full_name": data.full_name or current.full_name,
                        "email": data.email or current.email,
                    })
                    cells = {
                        "nombre_completo": record.full_name,
                        "email": record.email,
                        "fecha_actualizacion": now,
                    }
                    updates = [
                        {"range": rowcol_to_a1(row_number, table.columns[name] + 1), "values": [[value]]}
                        for name, value in cells.items()
                        if value and name in table.columns
                    ]
                    if updates:
                        ws.batch_update(updates, raw=True)
            except Exception as e:
                logger.error(f"Failed to save onboarding data for {mask_phone(phone)}: {e}")
                raise StorageError(f"Google Sheets onboarding write failed: {e}") from e

            # Write-through: the fresh read plus our change is the new snapshot.
            users = {k: r for k, (_, r) in table.rows.items()}
            users[key] = record
            self._cache.set(_SNAPSHOT_KEY, users)
            logger.info(f"Onboarding data saved for {mask_phone(phone)}.")
            return record

    @staticmethod
    def _build_row(columns: dict[str, int], values: dict[str, Any]) -> list:
        """Order values by the sheet's actual header positions, so reordered or extra columns are safe."""
        row = [""] * (max(columns.values()) + 1)
        for name, idx in columns.items():
            if name in values:
                row[idx] = values[name]
        return row
