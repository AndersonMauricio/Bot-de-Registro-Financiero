"""Unit tests for Evolution API webhook parsing (messages.upsert)."""

import pytest
from src.domain.enums import InputTypeEnum
from src.infrastructure.whatsapp.parser import WhatsAppPayloadParser


def test_parse_text_message(sample_webhook_text_payload):
    """Verify parsing of standard text messages."""
    msg = WhatsAppPayloadParser.parse_webhook(sample_webhook_text_payload)
    assert msg is not None
    assert msg.message_id == "3EB0A1B2C3D4E5F60001"
    assert msg.sender_phone == "5491122334455"
    assert msg.input_type == InputTypeEnum.TEXT
    assert msg.text_content == "Gasté 500 en Uber con crédito"


def test_parse_extended_text_message(make_evolution_payload):
    """Replies and links arrive as extendedTextMessage instead of conversation."""
    payload = make_evolution_payload(
        {"extendedTextMessage": {"text": "  pagué 200 de luz  "}}, message_type="extendedTextMessage"
    )
    msg = WhatsAppPayloadParser.parse_webhook(payload)
    assert msg is not None
    assert msg.input_type == InputTypeEnum.TEXT
    assert msg.text_content == "pagué 200 de luz"


def test_parse_audio_message_with_inline_base64(sample_webhook_audio_payload):
    """Verify voice notes keep the inline base64 Evolution attaches when webhook base64 is enabled."""
    msg = WhatsAppPayloadParser.parse_webhook(sample_webhook_audio_payload)
    assert msg is not None
    assert msg.message_id == "3EB0A1B2C3D4E5F60002"
    assert msg.sender_phone == "5491122334455"
    assert msg.input_type == InputTypeEnum.AUDIO
    assert msg.media_base64 == "T2dnUw=="
    assert msg.mime_type == "audio/ogg; codecs=opus"


def test_parse_audio_message_without_inline_base64(make_evolution_payload):
    """Without inline media the service falls back to downloading it by message id."""
    payload = make_evolution_payload({"audioMessage": {"mimetype": "audio/ogg; codecs=opus"}}, "audioMessage")
    msg = WhatsAppPayloadParser.parse_webhook(payload)
    assert msg is not None
    assert msg.input_type == InputTypeEnum.AUDIO
    assert msg.media_base64 is None


def test_parse_image_message_keeps_caption(make_evolution_payload):
    payload = make_evolution_payload(
        {"imageMessage": {"caption": " ticket del súper 4500 ", "mimetype": "image/jpeg"}, "base64": "AAAA"},
        message_type="imageMessage",
    )
    msg = WhatsAppPayloadParser.parse_webhook(payload)
    assert msg is not None
    assert msg.input_type == InputTypeEnum.IMAGE
    assert msg.text_content == "ticket del súper 4500"
    assert msg.media_base64 is None  # the MVP only uses the caption; no point carrying the image around


def test_ignores_messages_sent_by_the_bot_itself(make_evolution_payload):
    """Evolution echoes our own outgoing messages as messages.upsert; answering them would loop forever."""
    payload = make_evolution_payload({"conversation": "✅ Gasto registrado"}, from_me=True)
    assert WhatsAppPayloadParser.parse_webhook(payload) is None


@pytest.mark.parametrize(
    "remote_jid",
    ["120363025246125486@g.us", "status@broadcast", "123456789@newsletter", "5491122334455"],
)
def test_ignores_groups_status_and_non_phone_chats(make_evolution_payload, remote_jid):
    payload = make_evolution_payload({"conversation": "hola"}, remote_jid=remote_jid)
    assert WhatsAppPayloadParser.parse_webhook(payload) is None


def test_strips_device_suffix_from_jid(make_evolution_payload):
    payload = make_evolution_payload({"conversation": "hola"}, remote_jid="5491122334455:12@s.whatsapp.net")
    assert WhatsAppPayloadParser.parse_webhook(payload).sender_phone == "5491122334455"


def test_lid_contact_is_resolved_through_the_alternate_jid(make_evolution_payload):
    payload = make_evolution_payload(
        {"conversation": "hola"},
        remote_jid="98765432101234@lid",
        remoteJidAlt="5491122334455@s.whatsapp.net",
    )
    assert WhatsAppPayloadParser.parse_webhook(payload).sender_phone == "5491122334455"


def test_lid_contact_without_phone_is_ignored(make_evolution_payload):
    """Permissions are checked by phone number, so an unresolvable LID cannot be served."""
    payload = make_evolution_payload({"conversation": "hola"}, remote_jid="98765432101234@lid")
    assert WhatsAppPayloadParser.parse_webhook(payload) is None


def test_unwraps_disappearing_messages(make_evolution_payload):
    """Chats with disappearing messages nest the content under ephemeralMessage."""
    payload = make_evolution_payload(
        {"ephemeralMessage": {"message": {"extendedTextMessage": {"text": "gasté 300"}}}},
        message_type="ephemeralMessage",
    )
    msg = WhatsAppPayloadParser.parse_webhook(payload)
    assert msg is not None
    assert msg.text_content == "gasté 300"


def test_ignores_unsupported_message_types(make_evolution_payload):
    payload = make_evolution_payload({"stickerMessage": {"mimetype": "image/webp"}}, "stickerMessage")
    assert WhatsAppPayloadParser.parse_webhook(payload) is None


def test_ignores_empty_text(make_evolution_payload):
    assert WhatsAppPayloadParser.parse_webhook(make_evolution_payload({"conversation": "   "})) is None


def test_accepts_list_shaped_data(sample_webhook_text_payload):
    """Some Evolution versions deliver `data` as a list."""
    sample_webhook_text_payload["data"] = [sample_webhook_text_payload["data"]]
    assert WhatsAppPayloadParser.parse_webhook(sample_webhook_text_payload) is not None


@pytest.mark.parametrize(
    "payload",
    [{}, {"data": None}, {"data": []}, {"data": "oops"}, {"data": {"key": None}}, {"data": {"key": "x"}}],
)
def test_malformed_payloads_return_none(payload):
    assert WhatsAppPayloadParser.parse_webhook(payload) is None


@pytest.mark.parametrize(
    "event, expected",
    [
        ("messages.upsert", True),
        ("MESSAGES_UPSERT", True),
        ("messages.update", False),
        ("connection.update", False),
        ("", False),
    ],
)
def test_is_messages_upsert(event, expected):
    assert WhatsAppPayloadParser.is_messages_upsert({"event": event}) is expected
    assert WhatsAppPayloadParser.is_messages_upsert({}) is False


# ------------------------------------------------------- quoted replies (edit mode)
def test_a_text_reply_keeps_the_id_of_the_quoted_message(make_evolution_payload):
    """A correction arrives as a reply: `contextInfo.stanzaId` is the message the user answered."""
    payload = make_evolution_payload(
        {
            "extendedTextMessage": {
                "text": "Me equivoqué, eran 15000",
                "contextInfo": {
                    "stanzaId": "BOT-REPLY-1",
                    "participant": "5491100000000@s.whatsapp.net",
                },
            }
        },
        message_type="extendedTextMessage",
    )

    msg = WhatsAppPayloadParser.parse_webhook(payload)

    assert msg is not None
    assert msg.quoted_message_id == "BOT-REPLY-1"


def test_a_voice_note_reply_also_carries_the_quoted_message_id(make_evolution_payload):
    payload = make_evolution_payload(
        {
            "audioMessage": {
                "mimetype": "audio/ogg; codecs=opus",
                "contextInfo": {"stanzaId": "BOT-REPLY-2"},
            }
        },
        message_type="audioMessage",
    )

    msg = WhatsAppPayloadParser.parse_webhook(payload)

    assert msg is not None
    assert msg.input_type == InputTypeEnum.AUDIO
    assert msg.quoted_message_id == "BOT-REPLY-2"


def test_the_quote_survives_the_ephemeral_wrapper(make_evolution_payload):
    payload = make_evolution_payload(
        {
            "ephemeralMessage": {
                "message": {
                    "extendedTextMessage": {
                        "text": "eran 15000",
                        "contextInfo": {"stanzaId": "BOT-REPLY-3"},
                    }
                }
            }
        },
        message_type="ephemeralMessage",
    )

    msg = WhatsAppPayloadParser.parse_webhook(payload)

    assert msg is not None
    assert msg.text_content == "eran 15000"
    assert msg.quoted_message_id == "BOT-REPLY-3"


def test_a_message_that_quotes_nothing_has_no_quoted_id(sample_webhook_text_payload):
    parsed = WhatsAppPayloadParser.parse_webhook(sample_webhook_text_payload)
    assert parsed.quoted_message_id is None


def test_an_empty_stanza_id_is_treated_as_no_quote(make_evolution_payload):
    payload = make_evolution_payload(
        {"extendedTextMessage": {"text": "hola", "contextInfo": {"stanzaId": "   "}}},
        message_type="extendedTextMessage",
    )

    assert WhatsAppPayloadParser.parse_webhook(payload).quoted_message_id is None


def test_evolution_delivers_the_quote_at_data_level(make_evolution_payload):
    """Real Evolution v2.3.7 shape: `contextInfo` is a sibling of `message` inside `data`, not inside it."""
    payload = make_evolution_payload(
        {"conversation": "corrección, eso último eran en realidad 350"},
        message_id="3EB0CD0632EEF89514A5CE",
    )
    payload["data"]["contextInfo"] = {
        "participant": "21410495905957@lid",
        "stanzaId": "3EB0681853B22E12A9A66B",
        "quotedMessage": {"conversation": "✅ *Gasto Registrado con Éxito*\n\n💰 *Monto:* 160.00 USD"},
    }

    msg = WhatsAppPayloadParser.parse_webhook(payload)

    assert msg is not None
    assert msg.text_content == "corrección, eso último eran en realidad 350"
    assert msg.quoted_message_id == "3EB0681853B22E12A9A66B"


def test_a_data_level_context_without_a_stanza_id_is_not_a_quote(make_evolution_payload):
    payload = make_evolution_payload({"conversation": "gasté 160 usd en una cena"})
    payload["data"]["contextInfo"] = {"mentionedJid": [], "groupMentions": []}

    assert WhatsAppPayloadParser.parse_webhook(payload).quoted_message_id is None
