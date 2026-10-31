"""Unit tests for messages that describe several expenses in one go (batching)."""

from datetime import date
import pytest
from src.core.config import settings
from src.domain.enums import CategoryEnum, InputTypeEnum, PaymentMethodEnum
from src.domain.models import (
    EXPENSE_SHEET_HEADERS,
    ExtractedTransaction,
    ExtractionMetadata,
    LLMExtractionResponse,
)
from src.infrastructure.storage.sheets_repo import GoogleSheetsExpenseRepository
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services.data_quality import NO_EXPENSE_DETECTED_MESSAGE, DataQualityGate
from src.services.expense_service import ExpenseProcessingService
from tests.fakes import FakeExpenseRepository, FakeGroq, FakeWhatsApp, FakeWhisper, FakeWorksheet

pytestmark = pytest.mark.asyncio

PHONE = "5491100000001"
USER_HASH = DataQualityGate.hash_phone_number(PHONE)
TODAY = date(2026, 10, 28)


def tx(amount: float | None, category=CategoryEnum.ALIMENTACION, description: str = "Café", currency: str = "ARS"):
    return ExtractedTransaction(
        transaction_date=TODAY,
        amount=amount,
        currency=currency,
        category=category,
        subcategory=description,
        payment_method=PaymentMethodEnum.TARJETA_DEBITO,
        description=description,
    )


def two_expenses(confidence: float = 0.96) -> LLMExtractionResponse:
    """The spec example: pochoclos (20.000) + estacionamiento (5.000)."""
    return LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=confidence, raw_text="pochoclos y estacionamiento"),
        transactions=[
            tx(20000.0, CategoryEnum.ENTRETENIMIENTO, "Pochoclos"),
            tx(5000.0, CategoryEnum.TRANSPORTE, "Estacionamiento"),
        ],
    )


def message(text: str = "Anotá 20.000 de pochoclos y pagué 5.000 de estacionamiento", message_id="msg-batch-1"):
    return ParsedWhatsAppMessage(
        message_id=message_id,
        sender_phone=PHONE,
        timestamp="1729382400",
        input_type=InputTypeEnum.TEXT,
        text_content=text,
    )


@pytest.fixture
def parts():
    groq = FakeGroq()
    groq.expense_result = two_expenses()
    repo, whatsapp = FakeExpenseRepository(), FakeWhatsApp()
    service = ExpenseProcessingService(FakeWhisper(), groq, repo, whatsapp)
    return service, groq, repo, whatsapp


# ------------------------------------------------------------------ contract
async def test_the_contract_accepts_the_new_list_and_the_legacy_single_transaction():
    legacy = LLMExtractionResponse.model_validate(
        {
            "metadata": {"confidence_score": 0.9, "raw_text": "x"},
            "transaction": {
                "transaction_date": "2026-10-28",
                "amount": 100,
                "currency": "ARS",
                "category": "Transporte",
                "payment_method": "Efectivo",
                "description": "Uber",
            },
        }
    )
    batch = LLMExtractionResponse.model_validate(
        {
            "metadata": {"confidence_score": 0.9, "raw_text": "x"},
            "transactions": [
                {
                    "transaction_date": "2026-10-28",
                    "amount": 100,
                    "currency": "ARS",
                    "category": "Transporte",
                    "payment_method": "Efectivo",
                    "description": "Uber",
                }
            ],
        }
    )

    assert len(legacy.transactions) == 1 and legacy.transaction is not None
    assert len(batch.transactions) == 1 and batch.transaction is not None
    assert legacy.is_batch is False and batch.is_batch is False


async def test_an_empty_list_is_valid_and_means_the_message_is_not_an_expense():
    response = LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.0, raw_text="Hola bot"), transactions=[]
    )

    assert response.transactions == [] and response.transaction is None and response.is_batch is False
    [result] = DataQualityGate.validate_transactions(response, "MSG-1", PHONE, InputTypeEnum.TEXT)
    assert result.is_valid is False and result.no_expense_detected is True


# ------------------------------------------------------------------ gate
async def test_a_batch_gets_one_id_per_row_and_a_single_expense_keeps_the_message_id():
    single = LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.95, raw_text="a"), transactions=[tx(100)]
    )

    single_ids = [r.sanitized_record.message_id for r in DataQualityGate.validate_transactions(single, "MSG-1", PHONE, InputTypeEnum.TEXT)]
    batch_ids = [r.sanitized_record.message_id for r in DataQualityGate.validate_transactions(two_expenses(), "MSG-1", PHONE, InputTypeEnum.TEXT)]

    assert single_ids == ["MSG-1"]
    assert batch_ids == ["MSG-1-0", "MSG-1-1"]


async def test_one_invalid_expense_does_not_invalidate_the_rest():
    response = LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.95, raw_text="a"),
        transactions=[tx(20000.0), tx(-1.0), tx(5000.0)],
    )

    results = DataQualityGate.validate_transactions(response, "MSG-1", PHONE, InputTypeEnum.TEXT)

    assert [r.is_valid for r in results] == [True, False, True]
    assert "mayor a 0" in results[1].errors[0]


async def test_a_too_long_batch_is_rejected_with_an_actionable_message():
    response = LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.95, raw_text="a"),
        transactions=[tx(10.0) for _ in range(settings.MAX_TRANSACTIONS_PER_MESSAGE + 1)],
    )

    results = DataQualityGate.validate_transactions(response, "MSG-1", PHONE, InputTypeEnum.TEXT)

    assert len(results) == 1 and results[0].is_valid is False
    assert "mensajes separados" in results[0].errors[0]


# ------------------------------------------------------------------ flow
async def test_two_expenses_produce_two_rows_and_one_numbered_confirmation(parts):
    service, _, repo, whatsapp = parts

    result = await service.process_incoming_message(message())

    assert result.is_valid is True
    assert [r.amount for r in repo.saved] == [20000.0, 5000.0]
    assert [r.message_id for r in repo.saved] == ["msg-batch-1-0", "msg-batch-1-1"]
    text = whatsapp.texts[0]
    assert "Se registraron 2 gastos" in text
    assert "1. Entretenimiento (Pochoclos) — 20,000.00 ARS" in text
    assert "2. Transporte (Estacionamiento) — 5,000.00 ARS" in text
    assert "2026-10-28" in text
    assert "Tarjeta de Débito" in text


async def test_a_single_expense_keeps_the_usual_confirmation(parts, sample_valid_llm_response):
    service, groq, repo, whatsapp = parts
    groq.expense_result = sample_valid_llm_response

    await service.process_incoming_message(message("Almorcé sushi por 15.50 USD"))

    assert len(repo.saved) == 1
    assert "Gasto Registrado con Éxito" in whatsapp.texts[0]


async def test_the_confirmation_is_written_once_for_the_whole_batch(parts):
    service, _, repo, _ = parts

    await service.process_incoming_message(message())

    assert repo.reply_ids == {"msg-batch-1-0": "bot-reply-1", "msg-batch-1-1": "bot-reply-1"}


async def test_a_partially_invalid_batch_saves_the_valid_rows_and_explains_the_rest(parts):
    service, groq, repo, whatsapp = parts
    groq.expense_result = LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.95, raw_text="a"),
        transactions=[tx(20000.0, CategoryEnum.ENTRETENIMIENTO, "Pochoclos"), tx(-5.0, description="Mal monto")],
    )

    result = await service.process_incoming_message(message())

    assert [r.amount for r in repo.saved] == [20000.0]
    assert result.is_valid is False  # the batch is not fully registered
    text = whatsapp.texts[0]
    assert "Se registraron 1 de 2 gastos" in text
    assert "No pude registrar" in text
    assert "mayor a 0" in text


async def test_when_nothing_validates_the_user_is_told_nothing_was_saved(parts):
    service, groq, repo, whatsapp = parts
    groq.expense_result = two_expenses(confidence=0.5)  # unclear, but they do look like expenses

    await service.process_incoming_message(message())

    assert repo.saved == []
    assert "No pude registrar ninguno de los 2 gastos" in whatsapp.texts[0]


async def test_when_no_item_has_an_amount_the_user_gets_the_scope_explanation(parts):
    service, groq, repo, whatsapp = parts
    groq.expense_result = LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.9, raw_text="a"), transactions=[tx(None), tx(None)]
    )

    await service.process_incoming_message(message("compré cosas"))

    assert repo.saved == []
    assert whatsapp.texts == [NO_EXPENSE_DETECTED_MESSAGE]


async def test_a_missing_amount_in_one_item_does_not_hide_the_ones_that_were_saved(parts):
    service, groq, repo, whatsapp = parts
    groq.expense_result = LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.95, raw_text="a"),
        transactions=[tx(20000.0, CategoryEnum.ENTRETENIMIENTO, "Pochoclos"), tx(None, description="Estacionamiento")],
    )

    await service.process_incoming_message(message())

    assert [r.amount for r in repo.saved] == [20000.0]
    text = whatsapp.texts[0]
    assert "Se registraron 1 de 2 gastos" in text
    assert "Falta el monto del gasto" in text
    assert NO_EXPENSE_DETECTED_MESSAGE not in text


async def test_a_write_failure_reports_that_no_batch_row_was_saved(parts):
    service, _, repo, whatsapp = parts
    repo.fail_save = True

    await service.process_incoming_message(message())

    assert repo.saved == []
    assert "No pude guardar ninguno de los 2 gastos" in whatsapp.texts[0]
    assert "No se guardó nada" in whatsapp.texts[0]


async def test_the_repository_writes_the_batch_in_one_call():
    ws = FakeWorksheet([EXPENSE_SHEET_HEADERS])
    repo = GoogleSheetsExpenseRepository(worksheet=ws)
    groq = FakeGroq()
    groq.expense_result = two_expenses()
    whatsapp = FakeWhatsApp()
    service = ExpenseProcessingService(FakeWhisper(), groq, repo, whatsapp)

    await service.process_incoming_message(message())

    assert ws.append_rows_calls == [(2, "USER_ENTERED")]  # one request for both rows
    assert len(ws.rows) == 3
    stored = [dict(zip(EXPENSE_SHEET_HEADERS, row)) for row in ws.rows[1:]]
    assert [row["monto"] for row in stored] == [20000.0, 5000.0]
    assert [row["message_id"] for row in stored] == ["msg-batch-1-0", "msg-batch-1-1"]


# ------------------------------------------------------------------ corrections inside a batch
def batch_rows(repo: FakeExpenseRepository) -> None:
    """Two stored rows sharing the confirmation id, as a batch leaves them."""
    from src.domain.models import ExpenseRecord

    def row(message_id, amount, category, description):
        return ExpenseRecord(
            message_id=message_id,
            user_phone_hash=USER_HASH,
            input_type=InputTypeEnum.TEXT,
            transaction_date=TODAY,
            amount=amount,
            currency="ARS",
            category=category,
            subcategory=description,
            payment_method="Tarjeta de Débito",
            description=description,
            confidence_score=0.96,
            raw_text="batch",
        )

    repo.saved += [
        row("msg-batch-1-0", 20000.0, "Entretenimiento", "Pochoclos"),
        row("msg-batch-1-1", 5000.0, "Transporte", "Estacionamiento"),
    ]
    repo.reply_ids.update({"msg-batch-1-0": "BOT-BATCH", "msg-batch-1-1": "BOT-BATCH"})


def correction(text: str, quoted: str = "BOT-BATCH", message_id: str = "msg-corr") -> ParsedWhatsAppMessage:
    return ParsedWhatsAppMessage(
        message_id=message_id,
        sender_phone=PHONE,
        timestamp="1729382400",
        input_type=InputTypeEnum.TEXT,
        text_content=text,
        quoted_message_id=quoted,
    )


@pytest.fixture
def batch_parts():
    from src.domain.models import ExtractedTransaction

    groq = FakeGroq()
    groq.correction_result = LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.97, raw_text="eran 6000"),
        transactions=[
            ExtractedTransaction(
                transaction_date=TODAY,
                amount=6000.0,
                currency="ARS",
                category=CategoryEnum.TRANSPORTE,
                subcategory="Estacionamiento",
                payment_method=PaymentMethodEnum.TARJETA_DEBITO,
                description="Estacionamiento",
            )
        ],
    )
    repo, whatsapp = FakeExpenseRepository(), FakeWhatsApp()
    batch_rows(repo)
    service = ExpenseProcessingService(FakeWhisper(), groq, repo, whatsapp)
    return service, groq, repo, whatsapp


async def test_quoting_a_batch_confirmation_lists_the_expenses_to_choose(batch_parts):
    service, groq, repo, whatsapp = batch_parts

    await service.process_incoming_message(correction("me equivoqué"))

    assert repo.updated == []  # nothing changed yet
    assert groq.correction_calls == []
    text = whatsapp.texts[0]
    assert "tiene 2 gastos" in text
    assert "1. Entretenimiento (Pochoclos) — 20,000.00 ARS" in text
    assert "2. Transporte (Estacionamiento) — 5,000.00 ARS" in text


async def test_the_numbered_selection_corrects_only_that_row(batch_parts):
    service, groq, repo, whatsapp = batch_parts

    await service.process_incoming_message(correction("me equivoqué"))
    await service.process_incoming_message(message("el 2 eran 6000", message_id="msg-pick"))

    assert [r.message_id for r in repo.updated] == ["msg-batch-1-1"]
    assert repo.updated[0].amount == 6000.0
    assert "Registro modificado" in whatsapp.texts[-1]
    assert "antes 5,000.00 ARS" in whatsapp.texts[-1]
    assert groq.correction_calls[0][1]["description"] == "Estacionamiento"


async def test_the_selection_can_come_with_the_correction_in_the_same_message(batch_parts):
    service, _, repo, _ = batch_parts

    await service.process_incoming_message(correction('2) eran 6000'))

    assert [r.message_id for r in repo.updated] == ["msg-batch-1-1"]


async def test_a_bare_selection_asks_for_the_new_value_and_keeps_the_list(batch_parts):
    service, groq, repo, whatsapp = batch_parts

    await service.process_incoming_message(correction("me equivoqué"))
    await service.process_incoming_message(message("el 2", message_id="msg-pick"))
    assert repo.updated == [] and groq.correction_calls == []
    assert "¿Qué querés corregir del gasto 2" in whatsapp.texts[-1]

    await service.process_incoming_message(message("eran 6000", message_id="msg-value"))
    assert [r.message_id for r in repo.updated] == ["msg-batch-1-1"]


async def test_an_out_of_range_selection_is_rejected_without_changing_anything(batch_parts):
    service, _, repo, whatsapp = batch_parts

    await service.process_incoming_message(correction("me equivoqué"))
    await service.process_incoming_message(message("el 7 eran 6000", message_id="msg-pick"))

    assert repo.updated == []
    assert "No tengo una opción 7" in whatsapp.texts[-1]


async def test_a_message_that_is_not_a_selection_clears_the_list_and_is_processed_normally(batch_parts):
    service, groq, repo, whatsapp = batch_parts
    groq.expense_result = two_expenses()

    await service.process_incoming_message(correction("me equivoqué"))
    await service.process_incoming_message(message("Anotá 300 de propina", message_id="msg-new"))

    assert len(repo.saved) == 4  # the two original rows plus the two of the new message
    assert [r.message_id for r in repo.saved[2:]] == ["msg-new-0", "msg-new-1"]
