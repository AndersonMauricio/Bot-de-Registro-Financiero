"""Unit tests for correcting an already registered expense (update mode by citation)."""

from datetime import date
import pytest
from src.domain.enums import CategoryEnum, InputTypeEnum, PaymentMethodEnum
from src.domain.models import (
    ExpenseRecord,
    ExtractionMetadata,
    ExtractedTransaction,
    LLMExtractionResponse,
)
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services.data_quality import DataQualityGate
from src.services.expense_service import ExpenseProcessingService
from tests.fakes import FakeExpenseRepository, FakeGroq, FakeWhatsApp, FakeWhisper

pytestmark = pytest.mark.asyncio

PHONE = "5491100000001"
USER_HASH = DataQualityGate.hash_phone_number(PHONE)
ORIGINAL_MESSAGE_ID = "msg-original-1"
BOT_REPLY_ID = "BOT-REPLY-1"
CORRECTION_TEXT = "Me equivoqué, eran 15000"


def stored_record(**overrides) -> ExpenseRecord:
    """The expense the user registered before: $1.500 en un café del 2026-10-23."""
    values = dict(
        message_id=ORIGINAL_MESSAGE_ID,
        user_phone_hash=USER_HASH,
        input_type=InputTypeEnum.TEXT,
        transaction_date=date(2026, 10, 23),
        amount=1500.0,
        currency="ARS",
        category="Alimentación",
        subcategory="Café",
        payment_method="No Especificado",
        description="Café con amigos",
        confidence_score=0.95,
        raw_text="Gasté 1500 en café",
    )
    values.update(overrides)
    return ExpenseRecord(**values)


def correction_response(amount: float = 15000.0, confidence: float = 0.97) -> LLMExtractionResponse:
    return LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=confidence, raw_text=CORRECTION_TEXT),
        transaction=ExtractedTransaction(
            transaction_date=date(2026, 10, 23),
            amount=amount,
            currency="ARS",
            category=CategoryEnum.ALIMENTACION,
            subcategory="Café",
            payment_method=PaymentMethodEnum.NO_ESPECIFICADO,
            description="Café con amigos",
        ),
    )


def correction_message(
    quoted_id: str = BOT_REPLY_ID, text: str = CORRECTION_TEXT
) -> ParsedWhatsAppMessage:
    return ParsedWhatsAppMessage(
        message_id="msg-correction-1",
        sender_phone=PHONE,
        timestamp="1729382400",
        input_type=InputTypeEnum.TEXT,
        text_content=text,
        quoted_message_id=quoted_id,
    )


def new_expense_message(text: str = "Gasté 1500 en café") -> ParsedWhatsAppMessage:
    return ParsedWhatsAppMessage(
        message_id="msg-text-1",
        sender_phone=PHONE,
        timestamp="1729382400",
        input_type=InputTypeEnum.TEXT,
        text_content=text,
    )


@pytest.fixture
def parts():
    groq = FakeGroq()
    groq.correction_result = correction_response()
    repo = FakeExpenseRepository()
    repo.saved.append(stored_record())
    repo.processed.add(ORIGINAL_MESSAGE_ID)
    repo.reply_ids[ORIGINAL_MESSAGE_ID] = BOT_REPLY_ID  # the user answers the bot's confirmation
    whatsapp = FakeWhatsApp()
    whatsapp.next_reply_id = BOT_REPLY_ID
    service = ExpenseProcessingService(FakeWhisper(), groq, repo, whatsapp)
    return service, groq, repo, whatsapp


async def test_a_cited_message_updates_the_row_instead_of_inserting_a_new_one(parts):
    service, groq, repo, whatsapp = parts

    result = await service.process_incoming_message(correction_message())

    assert result.is_valid
    assert len(repo.saved) == 1  # nothing was appended
    assert [r.amount for r in repo.updated] == [15000.0]
    assert groq.expense_calls == []  # the correction never goes through the extraction prompt
    assert groq.correction_calls[0][0] == CORRECTION_TEXT
    assert "Registro modificado" in whatsapp.texts[0]


async def test_the_original_record_is_what_the_llm_receives_to_correct(parts):
    service, groq, _, _ = parts

    await service.process_incoming_message(correction_message())

    original = groq.correction_calls[0][1]
    assert original["amount"] == 1500.0
    assert original["transaction_date"] == "2026-10-23"
    assert original["category"] == "Alimentación"
    assert original["description"] == "Café con amigos"


async def test_the_corrected_row_keeps_the_original_message_id(parts):
    service, _, repo, _ = parts

    await service.process_incoming_message(correction_message())

    assert repo.updated[0].message_id == ORIGINAL_MESSAGE_ID
    assert repo.processed == {ORIGINAL_MESSAGE_ID}  # the correction id is never an idempotency key


async def test_the_confirmation_shows_the_new_amount_and_the_previous_one(parts):
    service, _, _, whatsapp = parts

    await service.process_incoming_message(correction_message())

    assert "15,000.00 ARS" in whatsapp.texts[0]
    assert "antes 1,500.00 ARS" in whatsapp.texts[0]
    assert "2026-10-23" in whatsapp.texts[0]


async def test_the_bot_answers_quoting_the_correction_and_links_its_reply(parts):
    service, _, repo, whatsapp = parts

    await service.process_incoming_message(correction_message())

    assert whatsapp.quoted_ids == ["msg-correction-1"]
    assert repo.reply_ids == {ORIGINAL_MESSAGE_ID: BOT_REPLY_ID}


async def test_a_correction_that_cites_a_foreign_expense_registers_nothing(parts):
    service, groq, repo, whatsapp = parts
    repo.saved[:] = [stored_record(user_phone_hash="b" * 64)]

    await service.process_incoming_message(correction_message())

    assert repo.updated == []
    assert len(repo.saved) == 1
    assert groq.correction_calls == []
    assert groq.expense_calls == []
    assert "No encontré un gasto tuyo" in whatsapp.texts[0]


async def test_an_unknown_citation_asks_for_clarification_instead_of_registering(parts):
    service, groq, repo, whatsapp = parts

    await service.process_incoming_message(correction_message(quoted_id="BOT-UNKNOWN"))

    assert repo.updated == []
    assert groq.expense_calls == []
    assert "No encontré un gasto tuyo" in whatsapp.texts[0]


async def test_a_vague_correction_is_rejected_by_the_data_quality_gate(parts):
    service, groq, repo, whatsapp = parts
    groq.correction_result = correction_response(amount=1500.0, confidence=0.35)

    results = await service.process_incoming_message(correction_message(text="Me equivoqué"))

    assert results.is_valid is False
    assert repo.updated == []
    assert "No pude aplicar la corrección" in whatsapp.texts[0]


async def test_a_lookup_failure_is_reported_without_touching_the_row(parts, monkeypatch):
    service, _, repo, whatsapp = parts

    def boom(*args, **kwargs):
        raise RuntimeError("sheets down")

    monkeypatch.setattr(repo, "find_expenses_by_message_id", boom)

    await service.process_incoming_message(correction_message())

    assert repo.updated == []
    assert "No pude consultar el registro" in whatsapp.texts[0]


async def test_an_llm_failure_during_a_correction_is_reported(parts):
    service, groq, repo, whatsapp = parts
    groq.error = RuntimeError("groq down")

    await service.process_incoming_message(correction_message())

    assert repo.updated == []
    assert "No pude interpretar la corrección" in whatsapp.texts[0]


async def test_a_correction_that_changes_nothing_is_not_written(parts):
    service, groq, repo, whatsapp = parts
    groq.correction_result = correction_response(amount=1500.0)  # same amount as the stored record

    await service.process_incoming_message(correction_message(text="era un café"))

    assert repo.updated == []
    assert "no encontré nada para corregir" in whatsapp.texts[0]


async def test_a_correction_that_does_not_mention_the_currency_keeps_the_stored_one(parts):
    """Real case: 'eran 155' on a 15 USD expense must not turn it into 155 ARS."""
    service, groq, repo, whatsapp = parts
    repo.saved[:] = [stored_record(amount=15.0, currency="USD", payment_method="Tarjeta de Débito")]
    groq.correction_result = LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.95, raw_text=CORRECTION_TEXT),
        transaction=ExtractedTransaction(
            transaction_date=date(2026, 10, 23),
            amount=155.0,
            currency="ARS",  # the model wrongly applied the default currency
            category=CategoryEnum.ALIMENTACION,
            subcategory="Café",
            payment_method=PaymentMethodEnum.TARJETA_DEBITO,
            description="Café con amigos",
        ),
    )

    await service.process_incoming_message(correction_message())

    assert repo.updated[0].amount == 155.0
    assert repo.updated[0].currency == "USD"  # preserved
    assert "155.00 USD" in whatsapp.texts[0]


async def test_a_mentioned_currency_does_change_the_stored_one(parts):
    service, groq, repo, _ = parts
    repo.saved[:] = [stored_record(amount=15.0, currency="ARS")]
    groq.correction_result = LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.95, raw_text=CORRECTION_TEXT),
        transaction=ExtractedTransaction(
            transaction_date=date(2026, 10, 23),
            amount=155.0,
            currency="USD",
            category=CategoryEnum.ALIMENTACION,
            subcategory="Café",
            payment_method=PaymentMethodEnum.NO_ESPECIFICADO,
            description="Café con amigos",
        ),
    )

    await service.process_incoming_message(correction_message(text="eran 155 dólares"))

    assert repo.updated[0].currency == "USD"  # mentioned: the guard does not revert it


async def test_a_correction_that_does_not_mention_the_date_keeps_the_stored_one(parts):
    service, groq, repo, _ = parts
    groq.correction_result = LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.95, raw_text=CORRECTION_TEXT),
        transaction=ExtractedTransaction(
            transaction_date=date(2026, 10, 28),  # "today", not mentioned by the user
            amount=15000.0,
            currency="ARS",
            category=CategoryEnum.ALIMENTACION,
            subcategory="Café",
            payment_method=PaymentMethodEnum.NO_ESPECIFICADO,
            description="Café con amigos",
        ),
    )

    await service.process_incoming_message(correction_message())

    assert repo.updated[0].transaction_date == date(2026, 10, 23)


async def test_a_row_that_disappeared_before_the_update_is_reported(parts, monkeypatch):
    service, _, repo, whatsapp = parts
    monkeypatch.setattr(repo, "update_expense", lambda record: False)

    await service.process_incoming_message(correction_message())

    assert "ya no existe" in whatsapp.texts[0]


async def test_a_new_expense_records_the_id_of_its_confirmation(parts, sample_valid_llm_response):
    service, groq, repo, _ = parts
    groq.expense_result = sample_valid_llm_response
    groq.correction_result = None
    repo.reply_ids.clear()

    await service.process_incoming_message(new_expense_message())

    assert repo.reply_ids == {"msg-text-1": BOT_REPLY_ID}


async def test_without_a_reply_id_the_expense_is_still_registered(parts, sample_valid_llm_response):
    service, groq, repo, whatsapp = parts
    groq.expense_result = sample_valid_llm_response
    groq.correction_result = None
    whatsapp.next_reply_id = None
    repo.reply_ids.clear()

    result = await service.process_incoming_message(new_expense_message())

    assert result.is_valid
    assert len(repo.saved) == 2
    assert repo.reply_ids == {}


async def test_stored_sheet_dates_are_normalized_before_going_to_the_llm():
    """Sheets may return the date in its locale format or as a serial: the model always gets ISO."""

    def as_transaction(stored_date):
        return ExpenseProcessingService._as_contract_transaction(
            {"fecha_gasto": stored_date, "monto": "1500", "moneda": "ARS"}
        )

    assert as_transaction("2026-10-23")["transaction_date"] == "2026-10-23"
    assert as_transaction("23/10/2026")["transaction_date"] == "2026-10-23"
    serial = (date(2026, 10, 23) - date(1899, 12, 30)).days
    assert as_transaction(serial)["transaction_date"] == "2026-10-23"
