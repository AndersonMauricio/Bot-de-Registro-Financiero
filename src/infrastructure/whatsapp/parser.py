"""Parser for WhatsApp Cloud API webhook payloads."""

from dataclasses import dataclass
from typing import Optional
from src.core.logging import logger
from src.domain.enums import InputTypeEnum


@dataclass
class ParsedWhatsAppMessage:
    """Standardized representation of an incoming WhatsApp message."""
    message_id: str
    sender_phone: str
    timestamp: str
    input_type: InputTypeEnum
    text_content: Optional[str] = None
    media_id: Optional[str] = None
    mime_type: Optional[str] = None


class WhatsAppPayloadParser:
    """Extracts relevant message data from Meta WhatsApp Cloud API webhooks."""

    @staticmethod
    def parse_webhook(payload: dict) -> Optional[ParsedWhatsAppMessage]:
        """Extract first valid user message from payload."""
        try:
            entry = payload.get("entry", [])[0]
            change = entry.get("changes", [])[0]
            value = change.get("value", {})
            messages = value.get("messages", [])

            if not messages:
                # Event might be status update (sent, delivered, read)
                return None

            msg = messages[0]
            msg_id = msg.get("id")
            sender = msg.get("from")
            timestamp = msg.get("timestamp")
            msg_type = msg.get("type")

            if not (msg_id and sender):
                return None

            # 1. Text Message
            if msg_type == "text":
                body = msg.get("text", {}).get("body", "").strip()
                return ParsedWhatsAppMessage(
                    message_id=msg_id,
                    sender_phone=sender,
                    timestamp=timestamp,
                    input_type=InputTypeEnum.TEXT,
                    text_content=body,
                )

            # 2. Audio or Voice Note
            if msg_type in ("audio", "voice"):
                audio_data = msg.get(msg_type, {})
                media_id = audio_data.get("id")
                mime_type = audio_data.get("mime_type", "audio/ogg")
                return ParsedWhatsAppMessage(
                    message_id=msg_id,
                    sender_phone=sender,
                    timestamp=timestamp,
                    input_type=InputTypeEnum.AUDIO,
                    media_id=media_id,
                    mime_type=mime_type,
                )

            # 3. Image / Ticket
            if msg_type == "image":
                image_data = msg.get("image", {})
                media_id = image_data.get("id")
                caption = image_data.get("caption", "")
                mime_type = image_data.get("mime_type", "image/jpeg")
                return ParsedWhatsAppMessage(
                    message_id=msg_id,
                    sender_phone=sender,
                    timestamp=timestamp,
                    input_type=InputTypeEnum.IMAGE,
                    media_id=media_id,
                    mime_type=mime_type,
                    text_content=caption,
                )

            logger.info(f"Ignored unsupported WhatsApp message type: {msg_type}")
            return None

        except (IndexError, KeyError, AttributeError) as e:
            logger.warning(f"Error parsing WhatsApp webhook payload structure: {e}")
            return None
