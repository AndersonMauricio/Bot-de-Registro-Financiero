"""Unit tests for domain models and data serialization."""

from datetime import date, time
from uuid import UUID
import pytest
from pydantic import ValidationError
from src.domain.enums import CategoryEnum, InputTypeEnum, PaymentMethodEnum
from src.domain.models import (
    EXPENSE_SHEET_HEADERS,
    OPTIONAL_EXPENSE_SHEET_HEADERS,
    REQUIRED_EXPENSE_SHEET_HEADERS,
    ExpenseRecord,
    ExtractedTransaction,
)


def make_record(**overrides) -> ExpenseRecord:
    values = dict(
        transaction_id=UUID("12345678-1234-5678-1234-567812345678"),
        message_id="wamid.test_001",
        user_phone_hash="a" * 64,
        input_type=InputTypeEnum.TEXT,
        transaction_date=date(2026, 10, 19),
        transaction_time=time(14, 30, 0),
        amount=120.75,
        currency="USD",
        category="Transporte",
        subcategory="Taxi",
        merchant_name="Cabify",
        payment_method="Tarjeta de Crédito",
        description="Viaje al aeropuerto",
        confidence_score=0.97,
        raw_text="Gasté 120.75 en Cabify al aeropuerto",
    )
    values.update(overrides)
    return ExpenseRecord(**values)


def test_sheets_schema_matches_the_agreed_columns():
    assert EXPENSE_SHEET_HEADERS == [
        "timestamp", "message_id", "user_phone_hash", "fecha_gasto", "monto",
        "moneda", "categoria", "subcategoria", "medio_de_pago", "descripcion",
        "reply_message_id",
    ]
    assert "reply_message_id" not in REQUIRED_EXPENSE_SHEET_HEADERS
    assert set(REQUIRED_EXPENSE_SHEET_HEADERS) | set(OPTIONAL_EXPENSE_SHEET_HEADERS) == set(EXPENSE_SHEET_HEADERS)


def test_expense_record_to_sheets_row():
    """Verify ExpenseRecord serializes to the columns of raw_expenses, in order (optional ones empty)."""
    row = make_record().to_sheets_row()

    assert len(row) == 11
    assert row[1] == "wamid.test_001"      # message_id (idempotency key)
    assert row[2] == "a" * 64              # user_phone_hash (never the phone itself)
    assert row[3] == "2026-10-19"          # fecha_gasto
    assert row[4] == 120.75                # monto
    assert row[5] == "USD"                 # moneda
    assert row[6] == "Transporte"          # categoria
    assert row[7] == "Taxi"                # subcategoria
    assert row[8] == "Tarjeta de Crédito"  # medio_de_pago
    assert row[9] == "Viaje al aeropuerto" # descripcion
    assert row[10] == ""                   # reply_message_id: written later, when the bot answers


def test_to_sheets_dict_and_row_agree():
    record = make_record()
    values = record.to_sheets_dict()
    assert list(values) == REQUIRED_EXPENSE_SHEET_HEADERS
    assert record.to_sheets_row() == [values.get(h, "") for h in EXPENSE_SHEET_HEADERS]


def test_sheet_never_contains_the_raw_phone_number():
    record = make_record(user_phone_hash="b" * 64)
    assert "5491122334455" not in [str(v) for v in record.to_sheets_row()]


BASE_TRANSACTION = dict(transaction_date=date(2026, 10, 19), amount=10.0, currency="ARS", description="Uber")


def test_payment_method_is_mandatory_in_the_extraction_contract():
    with pytest.raises(ValidationError):
        ExtractedTransaction(**BASE_TRANSACTION, category="Transporte")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Alimentación", CategoryEnum.ALIMENTACION),
        ("ALIMENTACION", CategoryEnum.ALIMENTACION),
        ("educacion", CategoryEnum.EDUCACION),
        ("Educación", CategoryEnum.EDUCACION),
        ("otros", CategoryEnum.OTROS),
    ],
)
def test_categories_accept_case_and_accent_variations_of_the_spanish_taxonomy(raw, expected):
    tx = ExtractedTransaction(**BASE_TRANSACTION, category=raw, payment_method="Efectivo")
    assert tx.category == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Efectivo", PaymentMethodEnum.EFECTIVO),
        ("tarjeta de credito", PaymentMethodEnum.TARJETA_CREDITO),
        ("TARJETA_DEBITO", PaymentMethodEnum.TARJETA_DEBITO),
        ("Transferencia", PaymentMethodEnum.TRANSFERENCIA),
        ("billetera virtual", PaymentMethodEnum.BILLETERA_VIRTUAL),
        ("No Especificado", PaymentMethodEnum.NO_ESPECIFICADO),
    ],
)
def test_payment_methods_accept_variations_of_the_spanish_taxonomy(raw, expected):
    assert ExtractedTransaction(**BASE_TRANSACTION, payment_method=raw).payment_method == expected


@pytest.mark.parametrize("category", ["Comida", "FOOD", "ALIMENTACION Y MAS", ""])
def test_categories_outside_the_taxonomy_are_rejected(category):
    with pytest.raises(ValidationError):
        ExtractedTransaction(**BASE_TRANSACTION, category=category, payment_method="Efectivo")


@pytest.mark.parametrize("method", ["CASH", "CREDIT_CARD", "Cripto", "UNKNOWN", ""])
def test_payment_methods_outside_the_taxonomy_are_rejected(method):
    """English legacy values are no longer valid: the contract is strictly Spanish."""
    with pytest.raises(ValidationError):
        ExtractedTransaction(**BASE_TRANSACTION, payment_method=method)


def test_taxonomies_are_stored_in_spanish():
    assert [c.value for c in CategoryEnum] == [
        "Alimentación", "Transporte", "Vivienda", "Servicios", "Salud",
        "Entretenimiento", "Educación", "Compras", "Otros",
    ]
    assert [m.value for m in PaymentMethodEnum] == [
        "Efectivo", "Tarjeta de Crédito", "Tarjeta de Débito", "Transferencia", "Billetera Virtual", "No Especificado",
    ]
