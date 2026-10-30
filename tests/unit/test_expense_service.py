"""Unit tests for the expense flow (approved users only): audio handling, typing presence and persistence."""

import asyncio
import base64
from datetime import date
import pytest
from src.core.exceptions import ExtractionError, TranscriptionError
from src.domain.enums import InputTypeEnum, PaymentMethodEnum
from src.domain.models import ExtractedTransaction, ExtractionMetadata, LLMExtractionResponse
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services.data_quality import NO_EXPENSE_DETECTED_MESSAGE
from src.services.expense_service import IMAGE_NOT_READABLE_MESSAGE, ExpenseProcessingService
from tests.fakes import FakeExpenseRepository, FakeGroq, FakeWhatsApp, FakeWhisper

pytestmark = pytest.mark.asyncio

PHONE = "5491100000001"
AUDIO_BYTES = b"OggS\x00fake-opus-audio"


def text_message(text: str = "Almorcé sushi por 15.50 USD pagando con débito") -> ParsedWhatsAppMessage:
    return ParsedWhatsAppMessage(
        message_id="msg-text-1",
        sender_phone=PHONE,
        timestamp="1729382400",
        input_type=InputTypeEnum.TEXT,
        text_content=text,
    )


def audio_message(inline: bool = True, mime_type: str = "audio/ogg; codecs=opus") -> ParsedWhatsAppMessage:
    return ParsedWhatsAppMessage(
        message_id="msg-audio-1",
        sender_phone=PHONE,
        timestamp="1729382400",
        input_type=InputTypeEnum.AUDIO,
        media_base64=base64.b64encode(AUDIO_BYTES).decode() if inline else None,
        mime_type=mime_type,
    )


@pytest.fixture
def parts(sample_valid_llm_response):
    groq = FakeGroq()
    groq.expense_result = sample_valid_llm_response
    whisper, repo, whatsapp = FakeWhisper(), FakeExpenseRepository(), FakeWhatsApp()
    service = ExpenseProcessingService(whisper, groq, repo, whatsapp)
    return service, groq, whisper, repo, whatsapp


async def test_text_message_is_extracted_saved_and_confirmed(parts):
    service, groq, whisper, repo, whatsapp = parts

    result = await service.process_incoming_message(text_message())

    assert result.is_valid
    assert len(repo.saved) == 1
    assert groq.expense_calls == ["Almorcé sushi por 15.50 USD pagando con débito"]
    assert whisper.calls == []
    assert "Gasto Registrado con Éxito" in whatsapp.texts[0]
    assert "15.50 USD" in whatsapp.texts[0]


async def test_typing_covers_the_processing_and_ends_before_the_reply_is_sent(parts):
    service, _, _, _, whatsapp = parts

    await service.process_incoming_message(text_message())

    assert [e[0] for e in whatsapp.events] == ["typing_start", "typing_end", "send"]
    assert whatsapp.events[0][1] == PHONE


async def test_inline_base64_audio_is_transcribed_without_any_download(parts):
    service, groq, whisper, _, whatsapp = parts
    whisper.text = "gasté quince con cincuenta en sushi"

    await service.process_incoming_message(audio_message(inline=True))

    assert whisper.calls == [(AUDIO_BYTES, "audio.ogg")]
    assert whatsapp.downloads == []
    assert groq.expense_calls == ["gasté quince con cincuenta en sushi"]


async def test_audio_without_inline_media_is_downloaded_from_evolution_by_message_id(parts):
    service, _, whisper, _, whatsapp = parts
    whatsapp.media["msg-audio-1"] = AUDIO_BYTES

    await service.process_incoming_message(audio_message(inline=False))

    assert whatsapp.downloads == ["msg-audio-1"]
    assert whisper.calls == [(AUDIO_BYTES, "audio.ogg")]


async def test_corrupt_inline_audio_falls_back_to_the_download(parts):
    service, _, whisper, _, whatsapp = parts
    whatsapp.media["msg-audio-1"] = AUDIO_BYTES
    msg = audio_message(inline=True)
    msg.media_base64 = "!!!not-base64!!!"

    await service.process_incoming_message(msg)

    assert whatsapp.downloads == ["msg-audio-1"]
    assert whisper.calls == [(AUDIO_BYTES, "audio.ogg")]


@pytest.mark.parametrize(
    "mime_type, filename",
    [
        ("audio/ogg; codecs=opus", "audio.ogg"),
        ("audio/mpeg", "audio.mp3"),
        ("audio/mp4", "audio.m4a"),
        (None, "audio.ogg"),
        ("audio/unknown", "audio.ogg"),
    ],
)
async def test_whisper_gets_a_filename_with_the_right_extension(parts, mime_type, filename):
    service, _, whisper, _, _ = parts

    await service.process_incoming_message(audio_message(mime_type=mime_type))

    assert whisper.calls[0][1] == filename


async def test_audio_that_cannot_be_obtained_or_transcribed_gets_a_friendly_error(parts):
    service, groq, whisper, repo, whatsapp = parts
    msg = audio_message(inline=False)  # nothing inline and nothing downloadable

    assert await service.process_incoming_message(msg) is None
    assert "No pudimos procesar el audio" in whatsapp.texts[-1]

    whatsapp.media["msg-audio-1"] = AUDIO_BYTES
    whisper.error = TranscriptionError("whisper down")
    assert await service.process_incoming_message(msg) is None
    assert "No pudimos procesar el audio" in whatsapp.texts[-1]
    assert groq.expense_calls == [] and repo.saved == []


async def test_silent_audio_gets_a_reply_instead_of_silence(parts):
    service, groq, whisper, _, whatsapp = parts
    whisper.text = "   "

    await service.process_incoming_message(audio_message())

    assert "No pudimos entender el mensaje" in whatsapp.texts[0]
    assert groq.expense_calls == []


async def test_image_without_caption_asks_for_text(parts):
    service, groq, _, _, whatsapp = parts
    msg = ParsedWhatsAppMessage("img-1", PHONE, "1", InputTypeEnum.IMAGE, text_content="", mime_type="image/jpeg")

    await service.process_incoming_message(msg)

    assert whatsapp.texts == [
        "📸 Recibimos tu imagen. Por ahora no puedo leer fotos o videos automáticamente. "
        "Por favor, escribime el detalle y el monto, o mandame un audio."
    ]
    assert whatsapp.texts[0] == IMAGE_NOT_READABLE_MESSAGE
    assert "MVP" not in whatsapp.texts[0]  # no internal jargon in front of the user
    assert groq.expense_calls == []


async def test_already_processed_message_is_skipped_silently_without_typing(parts):
    service, groq, _, repo, whatsapp = parts
    repo.processed.add("msg-text-1")

    assert await service.process_incoming_message(text_message()) is None

    assert whatsapp.events == []
    assert groq.expense_calls == []


async def test_low_confidence_extraction_is_not_saved_and_explains_why(parts):
    service, groq, _, repo, whatsapp = parts
    groq.expense_result = LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=0.3, raw_text="mmm"),
        transaction=groq.expense_result.transaction,
    )

    result = await service.process_incoming_message(text_message())

    assert result.is_valid is False
    assert repo.saved == []
    assert "No pude registrar tu gasto" in whatsapp.texts[0]


# ------------------------------------------------------- out of domain: not an expense, not a server error
def no_expense_response(confidence: float = 0.0, **extra) -> LLMExtractionResponse:
    return LLMExtractionResponse(
        metadata=ExtractionMetadata(confidence_score=confidence, raw_text="¿Qué es un mvp?"), **extra
    )


def amountless_transaction() -> ExtractedTransaction:
    return ExtractedTransaction(
        transaction_date=date(2026, 10, 19),
        currency="ARS",
        payment_method=PaymentMethodEnum.NO_ESPECIFICADO,
        description="Compras en el súper",
    )


def assert_scope_explanation(whatsapp, repo, result):
    assert result.is_valid is False
    assert repo.saved == []
    assert whatsapp.texts == [NO_EXPENSE_DETECTED_MESSAGE]
    assert "servidor" not in whatsapp.texts[0] and "⚠️" not in whatsapp.texts[0]


async def test_a_message_without_any_expense_gets_the_scope_explanation_instead_of_a_server_error(parts):
    service, groq, _, repo, whatsapp = parts
    groq.expense_result = no_expense_response()  # "transactions": [] is how the model says "not an expense"

    result = await service.process_incoming_message(text_message("¿Qué es un mvp?"))

    assert_scope_explanation(whatsapp, repo, result)
    assert whatsapp.texts[0] == (
        "No detecté ningún monto o gasto en tu mensaje. Recordá que solo puedo registrar transacciones "
        "financieras o generar tu resumen de gastos."
    )


async def test_a_very_low_confidence_extraction_gets_the_scope_explanation(parts):
    service, groq, _, repo, whatsapp = parts
    groq.expense_result = no_expense_response(0.1, transaction=groq.expense_result.transaction)

    result = await service.process_incoming_message(text_message("Hola bot"))

    assert_scope_explanation(whatsapp, repo, result)


async def test_an_expense_without_an_amount_gets_the_scope_explanation(parts):
    service, groq, _, repo, whatsapp = parts
    groq.expense_result = no_expense_response(0.9, transaction=amountless_transaction())

    result = await service.process_incoming_message(text_message("Gasté en el súper"))

    assert_scope_explanation(whatsapp, repo, result)


async def test_an_unclear_but_real_expense_still_asks_to_clarify_amount_and_concept(parts):
    """Between 'not an expense' and 'sure enough': the user is asked to clarify, not told the bot cannot help."""
    service, groq, _, repo, whatsapp = parts
    groq.expense_result = no_expense_response(0.5, transaction=groq.expense_result.transaction)

    await service.process_incoming_message(text_message("algo por ahí de 15 usd"))

    assert repo.saved == []
    assert "No pude registrar tu gasto" in whatsapp.texts[0]
    assert "aclarando el monto y concepto" in whatsapp.texts[0]
    assert NO_EXPENSE_DETECTED_MESSAGE not in whatsapp.texts[0]


async def test_a_real_llm_failure_is_still_reported_as_a_server_error_not_as_out_of_domain(parts):
    service, groq, _, repo, whatsapp = parts
    groq.error = ExtractionError("all models failed")

    await service.process_incoming_message(text_message("¿Qué es un mvp?"))

    assert "error en el servidor de inteligencia artificial" in whatsapp.texts[0]
    assert NO_EXPENSE_DETECTED_MESSAGE not in whatsapp.texts[0]


async def test_llm_failure_is_reported(parts):
    service, groq, _, repo, whatsapp = parts
    groq.error = ExtractionError("all models failed")

    assert await service.process_incoming_message(text_message()) is None

    assert "error en el servidor de inteligencia artificial" in whatsapp.texts[0]
    assert "no fue guardado" in whatsapp.texts[0]
    assert repo.saved == []


async def test_persistence_failure_is_reported(parts):
    service, _, _, repo, whatsapp = parts
    repo.fail_save = True

    await service.process_incoming_message(text_message())

    assert "No se guardó nada" in whatsapp.texts[0]
    assert "Gasto Registrado" not in whatsapp.texts[0]


async def test_blocking_sdk_calls_do_not_freeze_the_event_loop(parts):
    """The "typing..." presence is sent from the event loop while Groq works, so Groq must run in a thread."""
    service, groq, _, _, _ = parts
    groq.delay = 0.3  # a synchronous, blocking SDK call
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    await service.process_incoming_message(text_message())
    task.cancel()

    assert ticks >= 15  # the loop kept running during the 300 ms blocking call (it would be ~0 otherwise)


# ------------------------------------------------------- report requests are not expenses
class FakeReportService:
    def __init__(self):
        self.calls: list[str] = []

    async def handle(self, phone: str) -> str:
        self.calls.append(phone)
        return "📊 reporte listo"


@pytest.fixture
def with_reports(parts):
    service, groq, whisper, repo, whatsapp = parts
    reports = FakeReportService()
    service.report_service = reports
    return service, groq, whisper, repo, whatsapp, reports


async def test_a_text_report_request_is_answered_with_the_report_and_never_extracted_as_an_expense(with_reports):
    service, groq, _, repo, whatsapp, reports = with_reports

    result = await service.process_incoming_message(text_message("Quiero mi resumen"))

    assert result is None
    assert reports.calls == [PHONE]
    assert whatsapp.texts == ["📊 reporte listo"]
    assert groq.expense_calls == [] and repo.saved == []


async def test_a_voice_note_asking_for_the_report_is_transcribed_and_answered_too(with_reports):
    service, groq, whisper, repo, whatsapp, reports = with_reports
    whisper.text = "Quiero mi resumen de gastos"

    await service.process_incoming_message(audio_message())

    assert whisper.calls and reports.calls == [PHONE]
    assert groq.expense_calls == [] and repo.saved == []


async def test_typing_covers_the_report_generation_and_ends_before_the_reply(with_reports):
    service, _, _, _, whatsapp, _ = with_reports

    await service.process_incoming_message(text_message("dame mi reporte"))

    assert [e[0] for e in whatsapp.events] == ["typing_start", "typing_end", "send"]


async def test_a_real_expense_that_mentions_a_summary_still_goes_through_extraction(with_reports):
    service, groq, _, repo, _, reports = with_reports

    await service.process_incoming_message(text_message("Pagué 4500 pesos el resumen de la tarjeta"))

    assert reports.calls == []
    assert len(groq.expense_calls) == 1 and len(repo.saved) == 1


@pytest.mark.parametrize(
    "text",
    [
        "¿Cuánto gasté este mes?",
        "mis gastos",
        "Hola, ¿me podés pasar un resumen de todo lo que gasté?",
        "resumen de octubre 2026",
    ],
)
async def test_natural_language_report_requests_never_reach_the_expense_pipeline(with_reports, text):
    service, groq, _, repo, whatsapp, reports = with_reports

    await service.process_incoming_message(text_message(text))

    assert reports.calls == [PHONE]
    assert whatsapp.texts == ["📊 reporte listo"]
    assert groq.expense_calls == [] and repo.saved == []


async def test_without_a_report_service_report_requests_behave_as_before(parts):
    service, groq, _, _, _ = parts

    await service.process_incoming_message(text_message("Quiero mi resumen"))

    assert groq.expense_calls == ["Quiero mi resumen"]
