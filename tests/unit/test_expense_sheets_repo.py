"""Unit tests for the expenses Sheets repository: header-driven writes, idempotency and per-user reads."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import pytest
from src.core.exceptions import StorageError
from src.domain.enums import InputTypeEnum
from src.domain.models import EXPENSE_SHEET_HEADERS, ExpenseRecord
from src.infrastructure.storage import sheets_repo
from src.infrastructure.storage.sheets_repo import REPORT_COLUMNS, GoogleSheetsExpenseRepository
from tests.fakes import FakeWorksheet

HASH_A = "a" * 64
HASH_B = "b" * 64


def record(message_id: str, user_hash: str = HASH_A, amount: float = 10.0, **overrides) -> ExpenseRecord:
    values = dict(
        message_id=message_id,
        user_phone_hash=user_hash,
        input_type=InputTypeEnum.TEXT,
        transaction_date=date(2026, 10, 19),
        amount=amount,
        currency="ARS",
        category="Alimentación",
        subcategory="Supermercado",
        payment_method="Efectivo",
        description="Compras del súper",
        confidence_score=0.9,
        raw_text="gasté en el súper",
    )
    values.update(overrides)
    return ExpenseRecord(**values)


def empty_sheet() -> FakeWorksheet:
    return FakeWorksheet([EXPENSE_SHEET_HEADERS])


def repo_for(ws) -> GoogleSheetsExpenseRepository:
    return GoogleSheetsExpenseRepository(worksheet=ws)


# ------------------------------------------------------------------------ writes
def test_a_row_is_written_by_header_name_following_the_agreed_schema():
    ws = empty_sheet()

    repo_for(ws).save_expense(record("msg-1", amount=4500.5, subcategory="Supermercado", payment_method="Tarjeta de Débito"))

    stored = dict(zip(EXPENSE_SHEET_HEADERS, ws.rows[1]))
    assert stored["message_id"] == "msg-1"
    assert stored["user_phone_hash"] == HASH_A
    assert stored["fecha_gasto"] == "2026-10-19"
    assert stored["monto"] == 4500.5
    assert stored["moneda"] == "ARS"
    assert stored["categoria"] == "Alimentación"
    assert stored["subcategoria"] == "Supermercado"
    assert stored["medio_de_pago"] == "Tarjeta de Débito"
    assert stored["descripcion"] == "Compras del súper"
    assert len(stored["timestamp"]) == 19


def test_reordered_columns_and_extra_columns_are_respected():
    header = ["notas", *reversed(EXPENSE_SHEET_HEADERS)]
    ws = FakeWorksheet([header])

    repo_for(ws).save_expense(record("msg-1"))

    stored = dict(zip(header, ws.rows[1]))
    assert stored["notas"] == ""
    assert stored["message_id"] == "msg-1"
    assert stored["monto"] == 10.0


def test_identifier_columns_are_forced_to_text_so_sheets_cannot_turn_them_into_numbers():
    ws = empty_sheet()

    repo_for(ws).save_expense(record("123456789012345678901234", user_hash="1" * 63 + "e"))

    sent, _ = ws.appends[0]
    sent_by_name = dict(zip(EXPENSE_SHEET_HEADERS, sent))
    assert sent_by_name["message_id"].startswith("'")
    assert sent_by_name["user_phone_hash"].startswith("'")
    assert not str(sent_by_name["moneda"]).startswith("'")


def test_a_sheet_with_the_old_header_fails_loudly_instead_of_misaligning_data():
    ws = FakeWorksheet([["timestamp", "fecha_gasto", "monto", "moneda", "categoria", "descripcion"]])

    with pytest.raises(StorageError, match="message_id"):
        repo_for(ws).save_expense(record("msg-1"))

    assert ws.appends == []


def test_a_missing_column_is_reported_with_the_expected_header_row():
    ws = FakeWorksheet([[h for h in EXPENSE_SHEET_HEADERS if h != "medio_de_pago"]])

    with pytest.raises(StorageError) as exc:
        repo_for(ws).save_expense(record("msg-1"))

    assert "medio_de_pago" in str(exc.value)
    assert ", ".join(EXPENSE_SHEET_HEADERS) in str(exc.value)


def test_an_empty_worksheet_gets_the_agreed_header_row():
    ws = FakeWorksheet([])

    repo_for(ws)._ensure_headers(ws)

    assert ws.appends[0][0] == EXPENSE_SHEET_HEADERS


class SlowWorksheet(FakeWorksheet):
    """Records how many appends overlap in time."""

    def __init__(self):
        super().__init__([EXPENSE_SHEET_HEADERS])
        self.running = 0
        self.max_running = 0
        self._guard = threading.Lock()

    def append_row(self, values, value_input_option=None, **kwargs):
        with self._guard:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        time.sleep(0.03)
        super().append_row(values, value_input_option=value_input_option)
        with self._guard:
            self.running -= 1


def test_concurrent_saves_from_worker_threads_append_one_at_a_time():
    ws = SlowWorksheet()
    repo = repo_for(ws)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(repo.save_expense, [record(f"msg-{i}") for i in range(4)]))

    assert len(ws.rows) == 5
    assert ws.max_running == 1
    assert all(repo.is_message_processed(f"msg-{i}") for i in range(4))


def test_the_authenticated_client_is_the_shared_one(monkeypatch):
    shared = object()
    monkeypatch.setattr(sheets_repo, "get_gspread_client", lambda: shared)

    repo = GoogleSheetsExpenseRepository()

    assert repo._get_client() is shared
    monkeypatch.setattr(sheets_repo, "get_gspread_client", lambda: object())
    assert repo._get_client() is shared  # memoized per repository


# ------------------------------------------------------------------- idempotency
def test_processed_message_ids_survive_a_restart_because_they_are_read_from_the_sheet():
    ws = empty_sheet()
    repo_for(ws).save_expense(record("msg-1"))
    repo_for(ws).save_expense(record("msg-2"))

    after_restart = repo_for(ws)  # a new process: nothing in memory

    assert after_restart.is_message_processed("msg-1") is True
    assert after_restart.is_message_processed("msg-2") is True
    assert after_restart.is_message_processed("msg-3") is False


def test_the_message_id_column_is_read_once_not_per_message():
    ws = empty_sheet()
    ws.rows.append([""] * 1 + ["msg-old"] + [""] * 8)
    repo = repo_for(ws)

    for i in range(20):
        repo.is_message_processed(f"new-{i}")

    # one read for the header validation and one for the id column, no matter how many messages
    assert ws.reads == 2


def test_if_the_sheet_cannot_be_read_the_check_never_blocks_ingestion():
    ws = empty_sheet()
    ws.read_error = RuntimeError("429")
    repo = repo_for(ws)

    assert repo.is_message_processed("msg-1") is False  # does not raise


def test_a_saved_message_is_known_immediately_without_another_read():
    ws = empty_sheet()
    repo = repo_for(ws)
    repo.save_expense(record("msg-1"))
    reads = ws.reads

    assert repo.is_message_processed("msg-1") is True
    assert ws.reads == reads


# ---------------------------------------------------------- per-user reads (reports)
def test_a_user_only_gets_their_own_expenses():
    ws = empty_sheet()
    repo = repo_for(ws)
    repo.save_expense(record("m1", HASH_A, amount=100, description="mío 1"))
    repo.save_expense(record("m2", HASH_B, amount=999, description="de otro usuario"))
    repo.save_expense(record("m3", HASH_A, amount=200, description="mío 2"))

    mine = repo.get_expenses_by_user(HASH_A)
    theirs = repo.get_expenses_by_user(HASH_B)

    assert [r["descripcion"] for r in mine] == ["mío 1", "mío 2"]
    assert [r["monto"] for r in mine] == [100, 200]
    assert [r["descripcion"] for r in theirs] == ["de otro usuario"]


def test_the_report_rows_never_expose_the_message_id_or_the_hash():
    ws = empty_sheet()
    repo = repo_for(ws)
    repo.save_expense(record("m1", HASH_A))

    row = repo.get_expenses_by_user(HASH_A)[0]

    assert tuple(row) == REPORT_COLUMNS
    assert "message_id" not in row and "user_phone_hash" not in row and "timestamp" not in row


@pytest.mark.parametrize("hash_value", ["", None])
def test_an_empty_hash_never_matches_blank_cells_and_does_not_even_read_the_sheet(hash_value):
    ws = empty_sheet()
    ws.rows.append([""] * 10)  # a row without hash
    reads = ws.reads

    assert repo_for(ws).get_expenses_by_user(hash_value) == []
    assert ws.reads == reads


def test_a_partial_hash_does_not_match():
    ws = empty_sheet()
    repo = repo_for(ws)
    repo.save_expense(record("m1", HASH_A))

    assert repo.get_expenses_by_user(HASH_A[:32]) == []
    assert repo.get_expenses_by_user(HASH_A + "a") == []


def test_legacy_rows_without_a_hash_are_never_attributed_to_anybody():
    ws = empty_sheet()
    ws.rows.append(["2026-10-01 10:00:00", "old-1", "", "2026-10-01", 50, "ARS", "Salud", "", "Efectivo", "viejo"])
    repo = repo_for(ws)

    assert repo.get_expenses_by_user(HASH_A) == []


def test_short_rows_do_not_break_the_read():
    ws = empty_sheet()
    ws.rows.append(["2026-10-01 10:00:00", "m1", HASH_A])  # truncated row
    repo = repo_for(ws)

    rows = repo.get_expenses_by_user(HASH_A)

    assert rows == [{name: "" for name in REPORT_COLUMNS}]


def test_a_failing_sheet_raises_a_storage_error():
    ws = empty_sheet()
    repo = repo_for(ws)
    repo.save_expense(record("m1", HASH_A))
    ws.read_error = RuntimeError("503")

    with pytest.raises(StorageError):
        repo.get_expenses_by_user(HASH_A)
