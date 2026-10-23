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
    assert any("Confidence score" in err for err in result.errors)
    assert result.sanitized_record is None
