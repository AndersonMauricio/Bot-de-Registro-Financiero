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
            payment_method=PaymentMethodEnum.TARJETA_DEBITO,
            description="Almuerzo de sushi",
        ),
    )


@pytest.fixture
def make_evolution_payload():
    """Factory for Evolution API `messages.upsert` webhook payloads (all values are fictitious)."""

    def _make(
        message: dict,
        message_type: str = "conversation",
        remote_jid: str = "5491122334455@s.whatsapp.net",
        from_me: bool = False,
        message_id: str = "3EB0A1B2C3D4E5F60001",
        event: str = "messages.upsert",
        instance: str = "test-instance",
        **key_extra,
    ) -> dict:
        return {
            "event": event,
            "instance": instance,
            "data": {
                "key": {"remoteJid": remote_jid, "fromMe": from_me, "id": message_id, **key_extra},
                "pushName": "Usuario Demo",
                "status": "DELIVERY_ACK",
                "message": message,
                "messageType": message_type,
                "messageTimestamp": 1729382400,
                "instanceId": "00000000-0000-0000-0000-000000000000",
                "source": "android",
            },
            "destination": "https://example.com/api/v1/webhooks/evolution",
            "date_time": "2026-10-19T21:00:00.000Z",
            "sender": "5491100000000@s.whatsapp.net",
            "server_url": "http://evolution:8080",
            "apikey": "FAKE-INSTANCE-API-KEY",
        }

    return _make


@pytest.fixture
def sample_webhook_text_payload(make_evolution_payload) -> dict:
    """Fixture for an incoming text message from Evolution API."""
    return make_evolution_payload({"conversation": "Gasté 500 en Uber con crédito"})


@pytest.fixture
def sample_webhook_audio_payload(make_evolution_payload) -> dict:
    """Fixture for an incoming voice note from Evolution API, with the audio inline as base64 ("OggS")."""
    return make_evolution_payload(
        {
            "audioMessage": {"mimetype": "audio/ogg; codecs=opus", "seconds": 4, "ptt": True},
            "base64": "T2dnUw==",
        },
        message_type="audioMessage",
        message_id="3EB0A1B2C3D4E5F60002",
    )
