"""Unit tests for the Data Quality Gate and sanitization rules."""

from datetime import date
from src.domain.enums import CategoryEnum, InputTypeEnum, PaymentMethodEnum
from src.domain.models import ExtractedTransaction, ExtractionMetadata, LLMExtractionResponse
from src.services.data_quality import DataQualityGate


def test_hash_phone_number_pii():
    """Verify phone numbers are deterministically hashed via SHA-256 without punctuation."""
    phone = "+54 (911) 2233-4455"
    expected_hash = "f11a8bba107b34b1216a67448259db87cf7a917e3f89bf8ef897fbda58ba1d24"
    computed = DataQualityGate.hash_phone_number(phone)
    assert len(computed) == 64
    assert computed == DataQualityGate.hash_phone_number("5491122334455")


def test_sanitize_string_for_sheets_formula_injection():
    """Verify formula injection prefixes are escaped with a leading single quote."""
    assert DataQualityGate.sanitize_string_for_sheets("=SUM(A1:B10)") == "'=SUM(A1:B10)"
    assert DataQualityGate.sanitize_string_for_sheets("+54911") == "'+54911"
    assert DataQualityGate.sanitize_string_for_sheets("-100 USD") == "'-100 USD"
    assert DataQualityGate.sanitize_string_for_sheets("@evil_cmd") == "'@evil_cmd"
    assert DataQualityGate.sanitize_string_for_sheets("Normal Description") == "Normal Description"
    assert DataQualityGate.sanitize_string_for_sheets(None) == ""


def test_data_quality_gate_accepts_valid_record(sample_valid_llm_response):
    """Verify valid LLM responses pass the data quality gate with no errors."""
    result = DataQualityGate.validate_and_sanitize(
        llm_response=sample_valid_llm_response,
        message_id="msg_123",
        user_phone="5491100000000",
        input_type=InputTypeEnum.TEXT,
    )

    assert result.is_valid is True
    assert len(result.errors) == 0
    assert result.sanitized_record is not None
    assert result.sanitized_record.amount == 15.50
    assert result.sanitized_record.currency == "USD"
    assert result.sanitized_record.category == "Alimentación"
    assert result.sanitized_record.payment_method == "Tarjeta de Débito"
    assert result.sanitized_record.subcategory == "Restaurante"


def test_data_quality_gate_rejects_low_confidence():
    """Verify that extractions below confidence threshold are flagged as invalid."""
    low_confidence_response = LLMExtractionResponse(
        metadata=ExtractionMetadata(
            confidence_score=0.45,
            raw_text="No se entendió el audio",
        ),
        transaction=ExtractedTransaction(
            transaction_date=date(2026, 10, 19),
            amount=10.0,
            currency="USD",
            category=CategoryEnum.OTROS,
            subcategory="Otros",
            payment_method=PaymentMethodEnum.NO_ESPECIFICADO,
            description="Incierto",
        ),
    )

    result = DataQualityGate.validate_and_sanitize(
        llm_response=low_confidence_response,
        message_id="msg_456",
        user_phone="5491100000000",
        input_type=InputTypeEnum.AUDIO,
    )

    assert result.is_valid is False
    assert any("confianza" in err.lower() for err in result.errors)
    assert result.sanitized_record is None


# ------------------------------------------------ out of domain: no expense in the message
def _response(confidence: float, *transactions: ExtractedTransaction) -> LLMExtractionResponse:
    return LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=confidence, raw_text="mensaje"), transactions=list(transactions)
    )


def _transaction(amount) -> ExtractedTransaction:
    return ExtractedTransaction(
        transaction_date=date(2026, 10, 19),
        amount=amount,
        currency="ARS",
        category=CategoryEnum.OTROS,
        subcategory="Otros",
        payment_method=PaymentMethodEnum.NO_ESPECIFICADO,
        description="Algo",
    )


def _validate(response: LLMExtractionResponse):
    return DataQualityGate.validate_and_sanitize(response, "msg_1", "5491100000000", InputTypeEnum.TEXT)


def test_a_message_without_transactions_is_flagged_as_no_expense():
    result = _validate(_response(0.0))

    assert result.is_valid is False and result.no_expense_detected is True
    assert result.sanitized_record is None


def test_a_missing_amount_is_flagged_as_no_expense_with_its_own_error():
    result = _validate(_response(0.9, _transaction(None)))

    assert result.is_valid is False and result.no_expense_detected is True
    assert result.errors == ["Falta el monto del gasto."]


def test_a_zero_amount_is_flagged_as_no_expense():
    result = _validate(_response(0.9, _transaction(0.0)))

    assert result.is_valid is False and result.no_expense_detected is True


def test_a_very_low_confidence_is_flagged_as_no_expense_and_keeps_the_technical_error_for_the_logs():
    result = _validate(_response(0.1, _transaction(100.0)))

    assert result.is_valid is False and result.no_expense_detected is True
    assert any("confianza" in err.lower() for err in result.errors)


def test_an_unclear_expense_is_rejected_but_not_flagged_as_no_expense():
    """Confidence between the out-of-domain and the minimum thresholds: the user is asked to clarify."""
    below_minimum = DataQualityGate.MIN_CONFIDENCE_THRESHOLD - 0.01
    assert DataQualityGate.OUT_OF_DOMAIN_CONFIDENCE_THRESHOLD < below_minimum

    for confidence in (DataQualityGate.OUT_OF_DOMAIN_CONFIDENCE_THRESHOLD, 0.5, below_minimum):
        result = _validate(_response(confidence, _transaction(100.0)))
        assert result.is_valid is False and result.no_expense_detected is False, confidence


def test_a_valid_expense_is_never_flagged_as_no_expense():
    result = _validate(_response(0.95, _transaction(100.0)))

    assert result.is_valid is True and result.no_expense_detected is False


def test_the_scope_explanation_is_the_agreed_copy():
    from src.services.data_quality import NO_EXPENSE_DETECTED_MESSAGE

    assert NO_EXPENSE_DETECTED_MESSAGE == (
        "No detecté ningún monto o gasto en tu mensaje. Recordá que solo puedo registrar transacciones "
        "financieras o generar tu resumen de gastos."
    )
