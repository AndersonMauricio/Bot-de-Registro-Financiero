"""Unit tests for WhatsApp Cloud API webhook parsing."""

from src.domain.enums import InputTypeEnum
from src.infrastructure.whatsapp.parser import WhatsAppPayloadParser


def test_parse_text_message(sample_webhook_text_payload):
    """Verify parsing of standard text messages."""
    msg = WhatsAppPayloadParser.parse_webhook(sample_webhook_text_payload)
    assert msg is not None
    assert msg.message_id == "wamid.HBgLMTIzNDU2Nzg5MA=="
    assert msg.sender_phone == "5491122334455"
    assert msg.input_type == InputTypeEnum.TEXT
    assert msg.text_content == "Gasté 500 en Uber con crédito"


def test_parse_audio_message(sample_webhook_audio_payload):
    """Verify parsing of audio voice notes."""
    msg = WhatsAppPayloadParser.parse_webhook(sample_webhook_audio_payload)
    assert msg is not None
    assert msg.message_id == "wamid.HBgLMDAxMTIyMzM0NA=="
    assert msg.sender_phone == "5491122334455"
    assert msg.input_type == InputTypeEnum.AUDIO
    assert msg.media_id == "media_audio_id_999"


def test_parse_empty_or_status_payload():
    """Verify non-message event payloads return None gracefully."""
    status_payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "1", "changes": [{"value": {"statuses": [{"id": "wamid.123"}]}}]}]
    }
    msg = WhatsAppPayloadParser.parse_webhook(status_payload)
    assert msg is None
