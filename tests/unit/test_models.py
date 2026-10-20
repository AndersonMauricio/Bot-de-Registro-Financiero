"""Unit tests for domain models and data serialization."""

from datetime import date, time
from uuid import UUID
from src.domain.enums import InputTypeEnum
from src.domain.models import ExpenseRecord


def test_expense_record_to_sheets_row():
    """Verify ExpenseRecord correctly serializes to list matching Google Sheets column order."""
    record = ExpenseRecord(
        transaction_id=UUID("12345678-1234-5678-1234-567812345678"),
        message_id="wamid.test_001",
        user_phone_hash="a" * 64,
        input_type=InputTypeEnum.TEXT,
        transaction_date=date(2026, 10, 19),
        transaction_time=time(14, 30, 0),
        amount=120.75,
        currency="USD",
        category="TRANSPORTE",
        subcategory="Taxi",
        merchant_name="Cabify",
        payment_method="CREDIT_CARD",
        description="Viaje al aeropuerto",
        confidence_score=0.97,
        raw_text="Gasté 120.75 en Cabify al aeropuerto",
    )

    row = record.to_sheets_row()

    assert len(row) == 5
    assert row[1] == 120.75               # monto
    assert row[2] == "USD"                # moneda
    assert row[3] == "TRANSPORTE"         # categoria
    assert row[4] == "Viaje al aeropuerto" # descripcion
