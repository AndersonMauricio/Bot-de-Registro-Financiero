"""Graceful degradation: every external failure tells the user the truth and never fails silently."""

import time
import pytest
from src.core.async_utils import run_blocking
from src.core.config import settings
from src.core.exceptions import ExtractionError, ServiceTimeoutError, TranscriptionError
from src.domain.enums import InputTypeEnum
from src.domain.models import UserRecord
from src.infrastructure.audio.whisper_client import WhisperClient
from src.infrastructure.llm.groq_client import GroqLLMClient
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services import message_router as router_module
from src.services.expense_service import ExpenseProcessingService
from src.services.message_router import MessageRouter
from tests.fakes import FakeExpenseRepository, FakeGroq, FakeUserRepository, FakeWhatsApp, FakeWhisper

pytestmark = pytest.mark.asyncio

PHONE = "5491100000001"


def text_message(message_id: str = "msg-1") -> ParsedWhatsAppMessage:
    return ParsedWhatsAppMessage(
        message_id=message_id,
        sender_phone=PHONE,
        timestamp="1729382400",
        input_type=InputTypeEnum.TEXT,
        text_content="Gasté 1500 en café",
    )


def audio_message(message_id: str = "msg-audio") -> ParsedWhatsAppMessage:
    return ParsedWhatsAppMessage(
        message_id=message_id,
        sender_phone=PHONE,
        timestamp="1729382400",
        input_type=InputTypeEnum.AUDIO,
        media_base64=None,
        mime_type="audio/ogg",
    )


def service_with(groq=None, repo=None, whisper=None, whatsapp=None) -> ExpenseProcessingService:
    return ExpenseProcessingService(
        whisper or FakeWhisper(),
        groq or FakeGroq(),
        repo or FakeExpenseRepository(),
        whatsapp or FakeWhatsApp(),
    )


# ------------------------------------------------------------------ bounded blocking calls
async def test_a_blocking_call_that_exceeds_its_budget_raises_a_typed_timeout():
    with pytest.raises(ServiceTimeoutError) as exc:
        await run_blocking(time.sleep, 0.3, timeout=0.05, label="slow thing")

    assert "slow thing" in str(exc.value)


async def test_a_fast_blocking_call_returns_its_value():
    assert await run_blocking(lambda x: x * 2, 21, timeout=5, label="double") == 42


async def test_the_llm_and_whisper_sdks_are_configured_with_a_bounded_timeout():
    assert GroqLLMClient(api_key="test-key").client.timeout == settings.GROQ_TIMEOUT_SECONDS
    assert WhisperClient(api_key="test-key").client.timeout == settings.GROQ_TIMEOUT_SECONDS
    assert GroqLLMClient(api_key="test-key").client.max_retries == settings.GROQ_MAX_RETRIES


# ------------------------------------------------------------------ LLM failing
async def test_an_llm_outage_tells_the_user_the_expense_was_not_saved():
    groq, repo, whatsapp = FakeGroq(), FakeExpenseRepository(), FakeWhatsApp()
    groq.error = ExtractionError("503 Service Unavailable")

    await service_with(groq=groq, repo=repo, whatsapp=whatsapp).process_incoming_message(text_message())

    assert "error en el servidor de inteligencia artificial" in whatsapp.texts[0]
    assert "no fue guardado" in whatsapp.texts[0]
    assert repo.saved == []


async def test_an_llm_that_never_answers_is_bounded_and_reported(monkeypatch):
    monkeypatch.setattr(settings, "GROQ_TIMEOUT_SECONDS", 0.05, raising=False)
    groq, repo, whatsapp = FakeGroq(delay=0.3), FakeExpenseRepository(), FakeWhatsApp()

    await service_with(groq=groq, repo=repo, whatsapp=whatsapp).process_incoming_message(text_message())

    assert "tardó demasiado" in whatsapp.texts[0]
    assert repo.saved == []


# ------------------------------------------------------------------ storage failing
async def test_a_storage_outage_says_nothing_was_saved(sample_valid_llm_response):
    repo, whatsapp, groq = FakeExpenseRepository(), FakeWhatsApp(), FakeGroq()
    groq.expense_result = sample_valid_llm_response
    repo.fail_save = True

    await service_with(groq=groq, repo=repo, whatsapp=whatsapp).process_incoming_message(text_message())

    assert "No se guardó nada" in whatsapp.texts[0]
    assert "conservá el ticket" in whatsapp.texts[0].lower()


async def test_a_slow_storage_write_is_reported_as_unknown_not_as_failed(monkeypatch, sample_valid_llm_response):
    monkeypatch.setattr(settings, "STORAGE_TIMEOUT_SECONDS", 0.05, raising=False)
    repo, whatsapp, groq = FakeExpenseRepository(), FakeWhatsApp(), FakeGroq()
    groq.expense_result = sample_valid_llm_response
    original = repo.save_expenses

    def slow_save(records):
        time.sleep(0.3)
        return original(records)

    repo.save_expenses = slow_save

    await service_with(groq=groq, repo=repo, whatsapp=whatsapp).process_incoming_message(text_message())

    assert "no respondió a tiempo" in whatsapp.texts[0]
    assert "puede que tu gasto no se haya guardado" in whatsapp.texts[0]


async def test_a_slow_idempotency_read_does_not_block_ingestion(monkeypatch, sample_valid_llm_response):
    monkeypatch.setattr(settings, "STORAGE_TIMEOUT_SECONDS", 0.05, raising=False)
    repo, whatsapp, groq = FakeExpenseRepository(), FakeWhatsApp(), FakeGroq()
    groq.expense_result = sample_valid_llm_response

    def slow_check(message_id):
        time.sleep(0.3)
        return False

    repo.is_message_processed = slow_check

    await service_with(groq=groq, repo=repo, whatsapp=whatsapp).process_incoming_message(text_message())

    assert len(repo.saved) == 1  # the expense is registered anyway
    assert "Gasto Registrado" in whatsapp.texts[0]


# ------------------------------------------------------------------ audio failing
async def test_a_transcription_failure_says_nothing_was_registered():
    whisper, repo, whatsapp = FakeWhisper(), FakeExpenseRepository(), FakeWhatsApp()
    whisper.error = TranscriptionError("whisper down")
    whatsapp.media["msg-audio"] = b"OggS"

    await service_with(whisper=whisper, repo=repo, whatsapp=whatsapp).process_incoming_message(audio_message())

    assert "No se registró nada" in whatsapp.texts[0]
    assert repo.saved == []


async def test_audio_that_cannot_be_downloaded_is_reported_too():
    whatsapp = FakeWhatsApp()  # no media stored for that id: download_media raises
    repo = FakeExpenseRepository()

    await service_with(repo=repo, whatsapp=whatsapp).process_incoming_message(audio_message())

    assert "No se registró nada" in whatsapp.texts[0]
    assert repo.saved == []


# ------------------------------------------------------------------ nothing is ever silent
async def test_an_unexpected_error_still_gets_an_answer():
    class ExplodingService:
        async def process_incoming_message(self, parsed_msg):
            raise RuntimeError("boom")

    whatsapp = FakeWhatsApp()
    router = MessageRouter(
        user_repository=FakeUserRepository(
            {PHONE: UserRecord(phone=PHONE, status=1)}  # approved user
        ),
        expense_service=ExplodingService(),
        onboarding_service=None,
        whatsapp_client=whatsapp,
    )

    await router.handle(text_message())

    assert whatsapp.texts[-1] == router_module.UNEXPECTED_ERROR_MESSAGE


async def test_a_storage_read_failure_during_the_access_check_answers_with_a_warning():
    users, whatsapp = FakeUserRepository({PHONE: None}), FakeWhatsApp()
    users.fail_reads = True
    router = MessageRouter(
        user_repository=users,
        expense_service=service_with(),
        onboarding_service=None,
        whatsapp_client=whatsapp,
    )

    await router.handle(text_message())

    assert whatsapp.texts[-1] == router_module.ACCESS_CHECK_UNAVAILABLE_MESSAGE
