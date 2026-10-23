"""Unit tests for the expenses Sheets repository after moving its calls to worker threads."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from src.domain.enums import InputTypeEnum
from src.domain.models import ExpenseRecord
from src.infrastructure.storage import sheets_repo
from src.infrastructure.storage.sheets_repo import GoogleSheetsExpenseRepository


class SlowWorksheet:
    """Records how many appends overlap in time."""

    def __init__(self):
        self.rows = []
        self.running = 0
        self.max_running = 0
        self._guard = threading.Lock()

    def append_row(self, values, value_input_option=None, **kwargs):
        with self._guard:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        time.sleep(0.03)
        self.rows.append(values)
        with self._guard:
            self.running -= 1


def record(message_id: str) -> ExpenseRecord:
    return ExpenseRecord(
        message_id=message_id,
        user_phone_hash="a" * 64,
        input_type=InputTypeEnum.TEXT,
        transaction_date=date(2026, 10, 19),
        amount=10.0,
        currency="ARS",
        category="OTROS",
        subcategory="Otros",
        payment_method="CASH",
        description="Prueba",
        confidence_score=0.9,
        raw_text="prueba",
    )


def test_concurrent_saves_from_worker_threads_append_one_at_a_time():
    ws = SlowWorksheet()
    repo = GoogleSheetsExpenseRepository()
    repo._sheet = ws

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(repo.save_expense, [record(f"msg-{i}") for i in range(4)]))

    assert len(ws.rows) == 4
    assert ws.max_running == 1
    assert all(repo.is_message_processed(f"msg-{i}") for i in range(4))


def test_the_authenticated_client_is_the_shared_one(monkeypatch):
    shared = object()
    monkeypatch.setattr(sheets_repo, "get_gspread_client", lambda: shared)

    repo = GoogleSheetsExpenseRepository()

    assert repo._get_client() is shared
    monkeypatch.setattr(sheets_repo, "get_gspread_client", lambda: object())
    assert repo._get_client() is shared  # memoized per repository
