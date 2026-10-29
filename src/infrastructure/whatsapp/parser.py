"""Parser for Evolution API webhook payloads (`messages.upsert` events)."""

from dataclasses import dataclass
from typing import Optional
from src.core.logging import logger
from src.core.phone import normalize_phone
from src.domain.enums import InputTypeEnum


@dataclass
class ParsedWhatsAppMessage:
    """Standardized representation of an incoming WhatsApp message."""
    message_id: str
    sender_phone: str
    timestamp: str
    input_type: InputTypeEnum
    text_content: Optional[str] = None
    media_base64: Optional[str] = None
    mime_type: Optional[str] = None
    # Message this one answers (a quoted reply), when the user corrected an expense
    quoted_message_id: Optional[str] = None


class WhatsAppPayloadParser:
    """Extracts relevant message data from Evolution API webhooks."""

    # JID servers that identify a person (1:1 chat); groups are @g.us, status is @broadcast, etc.
    PHONE_JID_SERVERS = ("s.whatsapp.net", "c.us")
    # Disappearing-message and view-once chats nest the real content under one of these keys
    WRAPPER_KEYS = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2")

    @staticmethod
    def is_messages_upsert(payload: dict) -> bool:
        """The payload carries `messages.upsert`; the webhook config spells it MESSAGES_UPSERT."""
        return str(payload.get("event", "")).lower().replace("_", ".") == "messages.upsert"

    @classmethod
    def parse_webhook(cls, payload: dict) -> Optional[ParsedWhatsAppMessage]:
        """Extract the user message from a messages.upsert payload, or None if the bot must not answer it."""
        try:
            data = payload.get("data")
            if isinstance(data, list):
                data = data[0] if data else None
            if not isinstance(data, dict):
                return None

            key = data.get("key") or {}
            if key.get("fromMe"):
                # Evolution also emits our own outgoing messages; answering them would loop forever
                return None

            msg_id = key.get("id")
            sender = cls._sender_phone(key)
            if not (msg_id and sender):
                return None

            raw_message = data.get("message") or {}
            message = cls._unwrap(raw_message)
            timestamp = str(data.get("messageTimestamp", ""))
            # Evolution exposes the quote at data.contextInfo (sibling of `message`); real WhatsApp payloads
            # carry it inside the content node instead, so both places are checked
            quoted_id = cls._quoted_message_id(message, data.get("contextInfo"))

            # 1. Text Message
            if "conversation" in message or "extendedTextMessage" in message:
                body = message.get("conversation") or (message.get("extendedTextMessage") or {}).get("text")
                body = (body or "").strip()
                if not body:
                    return None
                return ParsedWhatsAppMessage(
                    message_id=msg_id,
                    sender_phone=sender,
                    timestamp=timestamp,
                    input_type=InputTypeEnum.TEXT,
                    text_content=body,
                    quoted_message_id=quoted_id,
                )

            # 2. Audio or Voice Note (base64 is inline when the webhook is configured with base64=true)
            if "audioMessage" in message:
                audio_data = message.get("audioMessage") or {}
                return ParsedWhatsAppMessage(
                    message_id=msg_id,
                    sender_phone=sender,
                    timestamp=timestamp,
                    input_type=InputTypeEnum.AUDIO,
                    media_base64=raw_message.get("base64") or None,
                    mime_type=audio_data.get("mimetype", "audio/ogg"),
                    quoted_message_id=quoted_id,
                )

            # 3. Image / Ticket
            if "imageMessage" in message:
                image_data = message.get("imageMessage") or {}
                return ParsedWhatsAppMessage(
                    message_id=msg_id,
                    sender_phone=sender,
                    timestamp=timestamp,
                    input_type=InputTypeEnum.IMAGE,
                    text_content=(image_data.get("caption") or "").strip(),
                    mime_type=image_data.get("mimetype", "image/jpeg"),
                    quoted_message_id=quoted_id,
                )

            logger.info(f"Ignored unsupported WhatsApp message type: {data.get('messageType')}")
            return None

        except (AttributeError, IndexError, KeyError, TypeError) as e:
            logger.warning(f"Error parsing WhatsApp webhook payload structure: {e}")
            return None

    @classmethod
    def _sender_phone(cls, key: dict) -> Optional[str]:
        """Digits of the 1:1 chat's phone. Groups, status broadcasts and unresolvable LIDs yield None."""
        jid = key.get("remoteJid") or ""
        if jid.endswith("@lid"):
            # WhatsApp "linked ID": the real number, when known, travels in an alternate field
            jid = key.get("remoteJidAlt") or key.get("senderPn") or ""
            if not jid:
                logger.warning("Ignored message from a LID contact whose phone number could not be resolved.")
                return None
        user, _, server = jid.partition("@")
        if server not in cls.PHONE_JID_SERVERS:
            return None
        return normalize_phone(user.split(":")[0]) or None  # strip the ":<device>" suffix

    @classmethod
    def _unwrap(cls, message: dict) -> dict:
        for _ in range(3):  # wrappers can be stacked, but never deeply
            for wrapper in cls.WRAPPER_KEYS:
                inner = (message.get(wrapper) or {}).get("message")
                if isinstance(inner, dict):
                    message = inner
                    break
            else:
                return message
        return message

    @staticmethod
    def _quoted_message_id(message: dict, context_info: Optional[dict] = None) -> Optional[str]:
        """ID of the message this one replies to (`contextInfo.stanzaId`), or None.

        Evolution delivers `contextInfo` at `data.contextInfo`; the raw WhatsApp payload nests it inside the
        content node (extendedTextMessage, audioMessage, imageMessage…), so every candidate is inspected.
        """
        candidates = [context_info, message.get("contextInfo")]
        candidates += [value.get("contextInfo") for value in message.values() if isinstance(value, dict)]
        for context in candidates:
            if not isinstance(context, dict):
                continue
            stanza_id = context.get("stanzaId") or (context.get("quotedMessage") or {}).get("stanzaId")
            if stanza_id:
                return str(stanza_id).strip() or None
        return None
