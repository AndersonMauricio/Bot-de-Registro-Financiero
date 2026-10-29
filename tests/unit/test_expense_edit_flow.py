"""End-to-end edit flow (no network): Evolution payload -> parser -> Sheets rows -> WhatsApp confirmation.

The repository is the real one, backed by an in-memory worksheet, so the `reply_message_id` wiring
(the ID of the bot's own answer) is exercised exactly as it will run against Google Sheets.
"""

from datetime import date
import pytest
from src.domain.enums import CategoryEnum, PaymentMethodEnum
from src.domain.models import (
    EXPENSE_SHEET_HEADERS,
    ExtractionMetadata,
    ExtractedTransaction,
    LLMExtractionResponse,
)
from src.infrastructure.storage.sheets_repo import GoogleSheetsExpenseRepository
from src.infrastructure.whatsapp.parser import WhatsAppPayloadParser
from src.services.expense_service import ExpenseProcessingService
from tests.fakes import FakeGroq, FakeWhatsApp, FakeWhisper, FakeWorksheet

pytestmark = pytest.mark.asyncio


def response(amount: float, description: str = "Café con amigos") -> LLMExtractionResponse:
    return LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.95, raw_text="Gasté 1500 en café"),
        transaction=ExtractedTransaction(
            transaction_date=date(2026, 10, 23),
            amount=amount,
            currency="ARS",
            category=CategoryEnum.ALIMENTACION,
            subcategory="Café",
            payment_method=PaymentMethodEnum.NO_ESPECIFICADO,
            description=description,
        ),
    )


def row(ws: FakeWorksheet) -> dict:
    return {name: str(value).lstrip("'") for name, value in zip(EXPENSE_SHEET_HEADERS, ws.rows[1])}


async def test_the_same_expense_can_be_corrected_twice_by_quoting_the_bot(make_evolution_payload):
    ws = FakeWorksheet([EXPENSE_SHEET_HEADERS])
    repo = GoogleSheetsExpenseRepository(worksheet=ws)
    groq = FakeGroq()
    groq.expense_result = response(1500.0)
    groq.correction_result = response(15000.0)
    whatsapp = FakeWhatsApp()
    whatsapp.next_reply_id = "BOT-REPLY-1"
    service = ExpenseProcessingService(FakeWhisper(), groq, repo, whatsapp)

    # 1. The original expense is registered and the confirmation id is stored in its row
    first = WhatsAppPayloadParser.parse_webhook(
        make_evolution_payload({"conversation": "Gasté 1500 en café"})
    )
    await service.process_incoming_message(first)

    assert len(ws.rows) == 2
    assert row(ws)["monto"] == "1500.0"
    assert row(ws)["message_id"] == "3EB0A1B2C3D4E5F60001"
    assert row(ws)["reply_message_id"] == "BOT-REPLY-1"

    # 2. The correction answers the bot's own message and updates the same row
    whatsapp.next_reply_id = "BOT-REPLY-2"
    second = WhatsAppPayloadParser.parse_webhook(
        make_evolution_payload(
            {
                "extendedTextMessage": {
                    "text": "Me equivoqué, eran 15000",
                    "contextInfo": {"stanzaId": "BOT-REPLY-1"},
                }
            },
            message_type="extendedTextMessage",
            message_id="USER-MSG-2",
        )
    )
    await service.process_incoming_message(second)

    assert len(ws.rows) == 2  # never a new row
    assert row(ws)["monto"] == "15000.0"
    assert row(ws)["message_id"] == "3EB0A1B2C3D4E5F60001"
    assert row(ws)["reply_message_id"] == "BOT-REPLY-2"
    assert "Registro modificado" in whatsapp.texts[-1]
    assert "antes 1,500.00 ARS" in whatsapp.texts[-1]

    # 3. A second correction can quote the newest confirmation
    groq.correction_result = response(15000.0, description="Café con amigos y medialunas")
    third = WhatsAppPayloadParser.parse_webhook(
        make_evolution_payload(
            {
                "extendedTextMessage": {
                    "text": "y eran con medialunas",
                    "contextInfo": {"stanzaId": "BOT-REPLY-2"},
                }
            },
            message_type="extendedTextMessage",
            message_id="USER-MSG-3",
        )
    )
    await service.process_incoming_message(third)

    assert len(ws.rows) == 2
    assert row(ws)["descripcion"] == "Café con amigos y medialunas"
    assert row(ws)["monto"] == "15000.0"


async def test_quoting_someone_elses_confirmation_does_not_touch_the_row(make_evolution_payload):
    ws = FakeWorksheet([EXPENSE_SHEET_HEADERS])
    repo = GoogleSheetsExpenseRepository(worksheet=ws)
    groq = FakeGroq()
    groq.expense_result = response(1500.0)
    whatsapp = FakeWhatsApp()
    service = ExpenseProcessingService(FakeWhisper(), groq, repo, whatsapp)

    first = WhatsAppPayloadParser.parse_webhook(
        make_evolution_payload(
            {"conversation": "Gasté 1500 en café"}, remote_jid="5491100000009@s.whatsapp.net"
        )
    )
    await service.process_incoming_message(first)
    owner_row = row(ws)

    # Another phone cites that confirmation
    groq.correction_result = response(1.0)
    intruder = WhatsAppPayloadParser.parse_webhook(
        make_evolution_payload(
            {
                "extendedTextMessage": {
                    "text": "eran 1",
                    "contextInfo": {"stanzaId": owner_row["reply_message_id"]},
                }
            },
            message_type="extendedTextMessage",
            message_id="USER-MSG-INTRUDER",
        )
    )
    await service.process_incoming_message(intruder)

    assert row(ws) == owner_row  # untouched
    assert len(ws.rows) == 2  # and no row is created for the intruder either
    assert "No encontré un gasto tuyo" in whatsapp.texts[-1]


async def test_a_quote_delivered_at_data_level_corrects_the_expense(make_evolution_payload):
    """End-to-end regression: the quote arrives in data.contextInfo (Evolution), not inside the message."""
    ws = FakeWorksheet([EXPENSE_SHEET_HEADERS])
    repo = GoogleSheetsExpenseRepository(worksheet=ws)
    groq = FakeGroq()
    groq.expense_result = response(160.0, description="Cena con tarjeta de débito")
    whatsapp = FakeWhatsApp()
    whatsapp.next_reply_id = "BOT-CONFIRM-160"
    service = ExpenseProcessingService(FakeWhisper(), groq, repo, whatsapp)

    await service.process_incoming_message(
        WhatsAppPayloadParser.parse_webhook(
            make_evolution_payload({"conversation": "gasté 160 usd en una cena con debito"})
        )
    )
    assert row(ws)["reply_message_id"] == "BOT-CONFIRM-160"

    groq.correction_result = response(350.0, description="Cena con tarjeta de débito")
    correcting = make_evolution_payload(
        {"conversation": "corrección, eso último eran en realidad 350"},
        message_id="3EB0CD0632EEF89514A5CE",
    )
    correcting["data"]["contextInfo"] = {
        "participant": "21410495905957@lid",
        "stanzaId": "BOT-CONFIRM-160",
        "quotedMessage": {"conversation": "✅ *Gasto Registrado con Éxito*"},
    }

    await service.process_incoming_message(WhatsAppPayloadParser.parse_webhook(correcting))

    assert len(ws.rows) == 2  # corrected in place
    assert row(ws)["monto"] == "350.0"
    assert row(ws)["moneda"] == "ARS"
    assert "Registro modificado" in whatsapp.texts[-1]
    assert "antes 160.00 ARS" in whatsapp.texts[-1]
