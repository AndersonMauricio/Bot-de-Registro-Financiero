"""Hand-written test doubles shared by the unit tests (no network, no Google, no Groq)."""

import time
from contextlib import asynccontextmanager
from typing import Optional
from gspread.utils import a1_to_rowcol
from src.core.exceptions import StorageError, WhatsAppClientError
from src.domain.models import (
    LLMExtractionResponse,
    OnboardingData,
    OnboardingLLMResponse,
    UserRecord,
)
from src.infrastructure.storage.base import ExpenseRepositoryInterface, UserRepositoryInterface


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWhatsApp:
    """Records everything the bot would do on WhatsApp, in order."""

    def __init__(self):
        self.events: list[tuple] = []  # ("typing_start", phone) | ("typing_end", phone) | ("send", phone, text)
        self.media: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.quoted_ids: list[Optional[str]] = []  # message answered by each reply, in order
        self.next_reply_id: Optional[str] = "bot-reply-1"

    @property
    def sent(self) -> list[tuple[str, str]]:
        return [(e[1], e[2]) for e in self.events if e[0] == "send"]

    @property
    def texts(self) -> list[str]:
        return [text for _, text in self.sent]

    @asynccontextmanager
    async def typing(self, phone: str):
        self.events.append(("typing_start", phone))
        try:
            yield
        finally:
            self.events.append(("typing_end", phone))

    async def send_text_message(self, phone: str, text: str, quoted_message_id: Optional[str] = None) -> bool:
        return bool(await self.send_reply(phone, text, quoted_message_id))

    async def send_reply(self, phone: str, text: str, quoted_message_id: Optional[str]) -> Optional[str]:
        self.events.append(("send", phone, text))
        self.quoted_ids.append(quoted_message_id)
        return self.next_reply_id

    async def download_media(self, message_id: str) -> bytes:
        self.downloads.append(message_id)
        if message_id not in self.media:
            raise WhatsAppClientError("media not found")
        return self.media[message_id]


class FakeUserRepository(UserRepositoryInterface):
    """In-memory users keyed by the exact phone string used in the test."""

    def __init__(self, users: Optional[dict[str, UserRecord]] = None):
        self.users: dict[str, UserRecord] = dict(users or {})
        self.saved: list[tuple[str, OnboardingData]] = []
        self.get_calls = 0
        self.fail_reads = False
        self.fail_writes = False
        self.invalidated: list[str] = []
        self.invalidated_all = 0

    def get_user(self, phone: str) -> Optional[UserRecord]:
        self.get_calls += 1
        if self.fail_reads:
            raise StorageError("users sheet down")
        return self.users.get(phone)

    def save_onboarding(self, phone: str, data: OnboardingData) -> UserRecord:
        if self.fail_writes:
            raise StorageError("users sheet write failed")
        self.saved.append((phone, data))
        current = self.users.get(phone)
        record = UserRecord(
            phone=phone,
            status=current.status if current else None,
            full_name=data.full_name or (current.full_name if current else None),
            email=data.email or (current.email if current else None),
        )
        self.users[phone] = record
        return record

    def invalidate(self, phone: str) -> None:
        self.invalidated.append(phone)

    def invalidate_all(self) -> None:
        self.invalidated_all += 1


class FakeGroq:
    """Stands in for GroqLLMClient. `delay` simulates a slow, blocking SDK call."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.error: Optional[Exception] = None
        self.onboarding_result = OnboardingLLMResponse()
        self.expense_result: Optional[LLMExtractionResponse] = None
        self.correction_result: Optional[LLMExtractionResponse] = None
        self.onboarding_calls: list[tuple[str, OnboardingData]] = []
        self.expense_calls: list[str] = []
        self.correction_calls: list[tuple[str, dict]] = []

    def collect_onboarding_data(self, text: str, collected: OnboardingData) -> OnboardingLLMResponse:
        self.onboarding_calls.append((text, collected))
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.onboarding_result

    def extract_expense_entities(self, text: str, reference_date=None, default_currency: str = "USD"):
        self.expense_calls.append(text)
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.expense_result

    def correct_expense_entities(
        self, text: str, original: dict, reference_date=None, default_currency: str = "ARS"
    ):
        self.correction_calls.append((text, original))
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.correction_result or self.expense_result


class FakeWhisper:
    def __init__(self, text: str = "gasté 500 en uber"):
        self.text = text
        self.error: Optional[Exception] = None
        self.calls: list[tuple[bytes, str]] = []

    def transcribe_audio_bytes(self, audio_bytes: bytes, filename: str = "audio.ogg", prompt=None) -> str:
        self.calls.append((audio_bytes, filename))
        if self.error:
            raise self.error
        return self.text


class FakeExpenseRepository(ExpenseRepositoryInterface):
    def __init__(self):
        self.saved = []
        self.processed: set[str] = set()
        self.fail_save = False
        self.fail_update = False
        self.queried_hashes: list[str] = []
        self.reply_ids: dict[str, str] = {}  # stored message_id -> id of the bot confirmation
        self.updated = []
        self.lookups: list[tuple[str, str]] = []

    def save_expense(self, record) -> str:
        if self.fail_save:
            raise StorageError("expense sheet down")
        self.saved.append(record)
        self.processed.add(record.message_id)
        return str(record.transaction_id)

    def is_message_processed(self, message_id: str) -> bool:
        return message_id in self.processed

    def get_expenses_by_user(self, user_phone_hash: str) -> list[dict]:
        self.queried_hashes.append(user_phone_hash)
        return [
            {
                "fecha_gasto": r.transaction_date.isoformat(),
                "monto": r.amount,
                "moneda": r.currency,
                "categoria": r.category,
                "subcategoria": r.subcategory,
                "medio_de_pago": r.payment_method,
                "descripcion": r.description,
            }
            for r in self.saved
            if r.user_phone_hash == user_phone_hash
        ]

    def find_expense_by_message_id(self, message_id: str, user_phone_hash: str) -> Optional[dict]:
        self.lookups.append((message_id, user_phone_hash))
        for record in self.saved:
            if record.user_phone_hash != user_phone_hash:
                continue
            if record.message_id == message_id or self.reply_ids.get(record.message_id) == message_id:
                return {
                    "fecha_gasto": record.transaction_date.isoformat(),
                    "monto": record.amount,
                    "moneda": record.currency,
                    "categoria": record.category,
                    "subcategoria": record.subcategory,
                    "medio_de_pago": record.payment_method,
                    "descripcion": record.description,
                    "message_id": record.message_id,
                    "reply_message_id": self.reply_ids.get(record.message_id, ""),
                }
        return None

    def update_expense(self, record) -> bool:
        if self.fail_update:
            raise StorageError("expense sheet down")
        for index, current in enumerate(self.saved):
            if current.message_id == record.message_id and current.user_phone_hash == record.user_phone_hash:
                self.saved[index] = record
                self.updated.append(record)
                return True
        return False

    def set_reply_message_id(self, message_id: str, reply_message_id: str, user_phone_hash: str) -> None:
        for record in self.saved:
            if record.message_id == message_id and record.user_phone_hash == user_phone_hash:
                self.reply_ids[message_id] = reply_message_id
                return


class FakeWorksheet:
    """Minimal in-memory stand-in for gspread.Worksheet (only what the repositories call).

    Like Google Sheets with USER_ENTERED, a leading apostrophe marks text and is not stored as part of the value.
    """

    def __init__(self, rows: list[list]):
        self.rows = [list(r) for r in rows]
        self.reads = 0
        self.appends: list[tuple[list, object]] = []  # (values as sent, value_input_option)
        self.batch_updates: list[list[dict]] = []
        self.read_error: Optional[Exception] = None

    def _padded(self) -> list[list]:
        width = max((len(r) for r in self.rows), default=0)
        return [r + [""] * (width - len(r)) for r in self.rows]

    def get_all_values(self, value_render_option=None, **kwargs) -> list[list]:
        self.reads += 1
        if self.read_error:
            raise self.read_error
        return self._padded()

    def row_values(self, row: int, **kwargs) -> list:
        self.reads += 1
        if self.read_error:
            raise self.read_error
        return list(self.rows[row - 1]) if len(self.rows) >= row else []

    def col_values(self, col: int, **kwargs) -> list:
        self.reads += 1
        if self.read_error:
            raise self.read_error
        return [r[col - 1] if len(r) >= col else "" for r in self.rows]

    def append_row(self, values, value_input_option=None, **kwargs) -> None:
        self.appends.append((list(values), value_input_option))
        stored = list(values)
        if str(value_input_option).endswith("USER_ENTERED") or value_input_option == "USER_ENTERED":
            stored = [v[1:] if isinstance(v, str) and v.startswith("'") else v for v in stored]
        self.rows.append(stored)

    def batch_update(self, data, raw: bool = True, **kwargs) -> None:
        self.batch_updates.append(data)
        for item in data:
            row, col = a1_to_rowcol(item["range"])
            self.rows[row - 1][col - 1] = item["values"][0][0]


class FakeReportWorksheet:
    """The first tab of a freshly created report spreadsheet."""

    def __init__(self):
        self.title = "Hoja 1"
        self.values: list[list] = []
        self.value_input_option = None
        self.frozen_rows = 0
        self.header_format = None
        self.size = None

    def update_title(self, title):
        self.title = title

    def resize(self, rows=None, cols=None):
        self.size = (rows, cols)

    def update(self, values, range_name=None, value_input_option=None, **kwargs):
        self.values = values
        self.value_input_option = value_input_option

    def freeze(self, rows=None, cols=None):
        self.frozen_rows = rows

    def format(self, range_name, fmt):
        self.header_format = (range_name, fmt)


class FakeSpreadsheet:
    def __init__(self, title: str, folder_id, fail_share: bool = False):
        self.title, self.folder_id = title, folder_id
        self.id = "sheet-123"
        self.url = "https://docs.google.com/spreadsheets/d/sheet-123"
        self.sheet1 = FakeReportWorksheet()
        self.shares: list[tuple] = []
        self._fail_share = fail_share

    def share(self, email_address, perm_type, role, **kwargs):
        if self._fail_share:
            raise RuntimeError("drive share failed")
        self.shares.append((email_address, perm_type, role))


class FakeGspreadClient:
    """Stands in for gspread.Client where reports are created (and cleaned up on failure)."""

    def __init__(self, fail_share: bool = False):
        self.created: list[FakeSpreadsheet] = []
        self.deleted: list[str] = []
        self._fail_share = fail_share

    def create(self, title, folder_id=None):
        spreadsheet = FakeSpreadsheet(title, folder_id, self._fail_share)
        self.created.append(spreadsheet)
        return spreadsheet

    def del_spreadsheet(self, file_id):
        self.deleted.append(file_id)


class FakeReportPublisher:
    """Stands in for GoogleSheetsReportPublisher."""

    def __init__(self, url: str = "https://docs.google.com/spreadsheets/d/fake"):
        self.url = url
        self.calls: list[tuple[str, list[dict], str]] = []
        self.error: Optional[Exception] = None

    def publish(self, title: str, rows: list[dict], share_with_email: str) -> str:
        if self.error:
            raise self.error
        self.calls.append((title, rows, share_with_email))
        return self.url
