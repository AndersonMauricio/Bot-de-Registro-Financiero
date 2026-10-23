"""Unit tests for the Drive/Sheets report publisher (no network: gspread is replaced by a fake client)."""

import pytest
from gspread.utils import ValueInputOption
from src.core.config import settings
from src.core.exceptions import StorageError
from src.infrastructure.storage import sheets_reports
from src.infrastructure.storage.sheets_reports import REPORT_HEADERS, GoogleSheetsReportPublisher
from tests.fakes import FakeGspreadClient

ROWS = [
    {"fecha_gasto": "2026-10-19", "monto": 1000.0, "moneda": "ARS", "categoria": "Alimentación",
     "subcategoria": "Supermercado", "medio_de_pago": "Efectivo", "descripcion": "Compras"},
    {"fecha_gasto": "2026-10-20", "monto": 15.5, "moneda": "USD", "categoria": "Transporte",
     "subcategoria": "Taxi", "medio_de_pago": "Tarjeta de Crédito", "descripcion": "=HYPERLINK(\"http://evil.example\")"},
]


@pytest.fixture
def client(monkeypatch):
    fake = FakeGspreadClient()
    monkeypatch.setattr(sheets_reports, "get_gspread_client", lambda: fake)
    return fake


def test_the_report_is_created_filled_and_shared_read_only_with_the_given_email(client):
    url = GoogleSheetsReportPublisher().publish("Reporte de prueba", ROWS, "lucia@example.com")

    spreadsheet = client.created[0]
    assert url == spreadsheet.url
    assert spreadsheet.title == "Reporte de prueba"
    assert spreadsheet.shares == [("lucia@example.com", "user", "reader")]  # read-only, to that user only
    sheet = spreadsheet.sheet1
    assert sheet.title == "Gastos"
    assert sheet.values[0] == REPORT_HEADERS
    assert sheet.values[1] == ["2026-10-19", 1000.0, "ARS", "Alimentación", "Supermercado", "Efectivo", "Compras"]
    assert sheet.frozen_rows == 1


def test_values_are_written_raw_so_nothing_in_the_report_is_evaluated_as_a_formula(client):
    GoogleSheetsReportPublisher().publish("t", ROWS, "lucia@example.com")

    assert client.created[0].sheet1.value_input_option == ValueInputOption.raw


def test_the_report_never_includes_the_message_id_the_hash_or_the_timestamp(client):
    GoogleSheetsReportPublisher().publish("t", ROWS, "lucia@example.com")

    flat = [str(v) for row in client.created[0].sheet1.values for v in row]
    assert not any("message_id" in v or "user_phone_hash" in v for v in flat)
    assert len(client.created[0].sheet1.values[0]) == 7


def test_the_report_is_created_in_the_configured_drive_folder(client, monkeypatch):
    monkeypatch.setattr(settings, "REPORTS_FOLDER_ID", "folder-xyz")

    GoogleSheetsReportPublisher().publish("t", ROWS, "lucia@example.com")

    assert client.created[0].folder_id == "folder-xyz"


def test_without_a_configured_folder_it_is_created_in_the_service_accounts_drive(client, monkeypatch):
    monkeypatch.setattr(settings, "REPORTS_FOLDER_ID", "")

    GoogleSheetsReportPublisher().publish("t", ROWS, "lucia@example.com")

    assert client.created[0].folder_id is None


def test_if_sharing_fails_the_half_built_file_is_deleted_and_an_error_is_raised(monkeypatch):
    fake = FakeGspreadClient(fail_share=True)
    monkeypatch.setattr(sheets_reports, "get_gspread_client", lambda: fake)

    with pytest.raises(StorageError):
        GoogleSheetsReportPublisher().publish("t", ROWS, "lucia@example.com")

    assert fake.deleted == ["sheet-123"]  # nothing is left behind, shared or not


def test_a_report_with_many_rows_resizes_the_sheet(client):
    rows = ROWS * 600

    GoogleSheetsReportPublisher().publish("t", rows, "lucia@example.com")

    assert client.created[0].sheet1.size == (len(rows) + 1, 7)
