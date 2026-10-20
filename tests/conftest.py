"""Pytest shared fixtures and mock configurations."""

import pytest
from datetime import date
from src.domain.enums import CategoryEnum, PaymentMethodEnum
from src.domain.models import ExtractedTransaction, ExtractionMetadata, LLMExtractionResponse


@pytest.fixture
def sample_valid_llm_response() -> LLMExtractionResponse:
    """Fixture providing a valid structured extraction from LLM."""
    return LLMExtractionResponse(
        metadata=ExtractionMetadata(
            confidence_score=0.95,
            raw_text="Almorcé sushi por 15.50 USD pagando con débito",
            extraction_notes=None,
        ),
        transaction=ExtractedTransaction(
            transaction_date=date(2026, 10, 19),
            transaction_time=None,
            amount=15.50,
            currency="USD",
            category=CategoryEnum.ALIMENTACION,
            subcategory="Restaurante",
            merchant_name="Sushi Bar",
            payment_method=PaymentMethodEnum.DEBIT_CARD,
            description="Almuerzo de sushi",
        ),
    )


@pytest.fixture
def sample_webhook_text_payload() -> dict:
    """Fixture for an incoming text message payload from WhatsApp Cloud API."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456789",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15551234567",
                                "phone_number_id": "987654321",
                            },
                            "messages": [
                                {
                                    "from": "5491122334455",
                                    "id": "wamid.HBgLMTIzNDU2Nzg5MA==",
                                    "timestamp": "1729382400",
                                    "text": {"body": "Gasté 500 en Uber con crédito"},
                                    "type": "text",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


@pytest.fixture
def sample_webhook_audio_payload() -> dict:
    """Fixture for an incoming voice note payload from WhatsApp Cloud API."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456789",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": "5491122334455",
                                    "id": "wamid.HBgLMDAxMTIyMzM0NA==",
                                    "timestamp": "1729382410",
                                    "type": "audio",
                                    "audio": {
                                        "id": "media_audio_id_999",
                                        "mime_type": "audio/ogg; codecs=opus",
                                    },
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
