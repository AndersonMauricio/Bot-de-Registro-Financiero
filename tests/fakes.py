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

    async def send_text_message(self, phone: str, text: str) -> bool:
        self.events.append(("send", phone, text))
        return True

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
            full_name=data.full_name,
            email=data.email,
        )
        self.users[phone] = record
        return record


class FakeGroq:
    """Stands in for GroqLLMClient. `delay` simulates a slow, blocking SDK call."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.error: Optional[Exception] = None
        self.onboarding_result = OnboardingLLMResponse()
        self.expense_result: Optional[LLMExtractionResponse] = None
        self.onboarding_calls: list[tuple[str, OnboardingData]] = []
        self.expense_calls: list[str] = []

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

    def save_expense(self, record) -> str:
        if self.fail_save:
            raise StorageError("expense sheet down")
        self.saved.append(record)
        self.processed.add(record.message_id)
        return str(record.transaction_id)

    def is_message_processed(self, message_id: str) -> bool:
        return message_id in self.processed


class FakeWorksheet:
    """Minimal in-memory stand-in for gspread.Worksheet (only what the users repository calls)."""

    def __init__(self, rows: list[list]):
        self.rows = [list(r) for r in rows]
        self.reads = 0
        self.appends: list[tuple[list, object]] = []  # (values, value_input_option)
        self.batch_updates: list[list[dict]] = []
        self.read_error: Optional[Exception] = None

    def get_all_values(self, value_render_option=None, **kwargs) -> list[list]:
        self.reads += 1
        if self.read_error:
            raise self.read_error
        width = max((len(r) for r in self.rows), default=0)
        return [r + [""] * (width - len(r)) for r in self.rows]

    def append_row(self, values, value_input_option=None, **kwargs) -> None:
        self.appends.append((list(values), value_input_option))
        self.rows.append(list(values))

    def batch_update(self, data, raw: bool = True, **kwargs) -> None:
        self.batch_updates.append(data)
        for item in data:
            row, col = a1_to_rowcol(item["range"])
            self.rows[row - 1][col - 1] = item["values"][0][0]
