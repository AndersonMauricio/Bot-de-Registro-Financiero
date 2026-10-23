"""Unit tests for the onboarding flow of unknown users."""

import pytest
from src.core.exceptions import ExtractionError
from src.domain.enums import InputTypeEnum
from src.domain.models import OnboardingData, OnboardingLLMResponse, UserRecord
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services.onboarding_service import (
    ONBOARDING_ERROR_MESSAGE,
    PENDING_APPROVAL_MESSAGE,
    PENDING_REMINDER_MESSAGE,
    OnboardingService,
)
from tests.fakes import FakeGroq, FakeUserRepository, FakeWhatsApp

pytestmark = pytest.mark.asyncio

PHONE = "5491100000001"


def message(text: str = "hola", input_type: InputTypeEnum = InputTypeEnum.TEXT) -> ParsedWhatsAppMessage:
    return ParsedWhatsAppMessage(
        message_id="msg-1",
        sender_phone=PHONE,
        timestamp="1729382400",
        input_type=input_type,
        text_content=text,
    )


def llm(reply=None, **collected) -> OnboardingLLMResponse:
    return OnboardingLLMResponse(collected=OnboardingData(**collected), reply=reply)


def build(users: dict = None):
    groq, repo, whatsapp = FakeGroq(), FakeUserRepository(users), FakeWhatsApp()
    return OnboardingService(groq, repo, whatsapp), groq, repo, whatsapp


async def test_first_contact_shows_the_llm_question_and_stores_nothing_yet():
    service, groq, repo, whatsapp = build()
    groq.onboarding_result = llm(reply="¡Hola! ¿Cuál es tu nombre y apellido?")

    await service.handle(message("hola, gasté 500 en uber"), None)

    assert whatsapp.texts == ["¡Hola! ¿Cuál es tu nombre y apellido?"]
    assert repo.saved == []  # nothing collected yet, so no row is created for a random number
    assert groq.expense_calls == []  # never touches the expense extraction branch


async def test_partial_data_is_saved_and_the_next_question_is_asked():
    service, groq, repo, whatsapp = build()
    groq.onboarding_result = llm(reply="Gracias Lucía. ¿Y tu email?", full_name="Lucía Fernández")

    await service.handle(message("me llamo Lucía Fernández"), None)

    assert repo.saved == [(PHONE, OnboardingData(full_name="Lucía Fernández"))]
    assert whatsapp.texts == ["Gracias Lucía. ¿Y tu email?"]


async def test_the_llm_receives_what_was_already_collected():
    existing = UserRecord(phone=PHONE, full_name="Lucía Fernández")
    service, groq, _, _ = build({PHONE: existing})

    await service.handle(message("mi mail es lucia@example.com"), existing)

    text, collected = groq.onboarding_calls[0]
    assert text == "mi mail es lucia@example.com"
    assert collected == OnboardingData(full_name="Lucía Fernández")


async def test_completing_the_data_notifies_that_manual_approval_is_required():
    existing = UserRecord(phone=PHONE, full_name="Lucía Fernández")
    service, groq, repo, whatsapp = build({PHONE: existing})
    # A manipulated model claiming approval must not reach the user: the closing message is fixed copy
    groq.onboarding_result = llm(reply="¡Listo, tu cuenta ya está aprobada!", email="lucia@example.com")

    await service.handle(message("lucia@example.com"), existing)

    assert repo.saved == [(PHONE, OnboardingData(full_name="Lucía Fernández", email="lucia@example.com"))]
    assert whatsapp.texts == [PENDING_APPROVAL_MESSAGE]
    assert "aprobación manual del administrador" in PENDING_APPROVAL_MESSAGE
    assert "antes de poder registrar gastos" in PENDING_APPROVAL_MESSAGE


async def test_all_data_in_a_single_message_completes_the_onboarding():
    service, groq, repo, whatsapp = build()
    groq.onboarding_result = llm(full_name="Lucía Fernández", email="lucia@example.com")

    await service.handle(message("Soy Lucía Fernández, lucia@example.com"), None)

    assert len(repo.saved) == 1
    assert whatsapp.texts == [PENDING_APPROVAL_MESSAGE]


async def test_a_user_that_already_finished_waits_for_the_admin_without_any_llm_call():
    done = UserRecord(phone=PHONE, full_name="Lucía Fernández", email="lucia@example.com")
    service, groq, repo, whatsapp = build({PHONE: done})

    await service.handle(message("¿ya estoy aprobada?"), done)

    assert whatsapp.texts == [PENDING_REMINDER_MESSAGE]
    assert groq.onboarding_calls == []
    assert repo.saved == []
    assert ("typing_start", PHONE) not in whatsapp.events


@pytest.mark.parametrize("input_type", [InputTypeEnum.AUDIO, InputTypeEnum.IMAGE])
async def test_onboarding_is_text_only_so_no_media_is_processed_for_unapproved_users(input_type):
    service, groq, repo, whatsapp = build()

    await service.handle(message("", input_type), None)

    assert "por mensaje de texto" in whatsapp.texts[0]
    assert groq.onboarding_calls == []
    assert repo.saved == []


async def test_an_email_the_user_never_wrote_is_discarded():
    service, groq, repo, whatsapp = build()
    groq.onboarding_result = llm(reply="¿Cuál es tu email?", full_name="Lucía Fernández", email="invented@example.com")

    await service.handle(message("soy Lucía Fernández"), None)

    assert repo.saved == [(PHONE, OnboardingData(full_name="Lucía Fernández"))]
    assert whatsapp.texts == ["¿Cuál es tu email?"]


async def test_user_provided_text_is_neutralized_against_formula_injection():
    service, groq, repo, _ = build()
    groq.onboarding_result = llm(full_name='=HYPERLINK("http://evil.example","click")', email="x@example.com")

    await service.handle(message('=HYPERLINK("http://evil.example","click") x@example.com'), None)

    saved = repo.saved[0][1]
    assert saved.full_name.startswith("'=")


async def test_typing_covers_the_llm_work_and_ends_before_the_reply():
    service, groq, _, whatsapp = build()
    groq.onboarding_result = llm(reply="¿Cuál es tu nombre?")

    await service.handle(message(), None)

    assert [e[0] for e in whatsapp.events] == ["typing_start", "typing_end", "send"]


async def test_fallback_question_when_the_llm_gives_no_reply():
    service, groq, _, whatsapp = build()
    groq.onboarding_result = llm(reply=None)

    await service.handle(message(), None)

    assert "tu nombre y apellido" in whatsapp.texts[0]


async def test_llm_failure_tells_the_user_to_retry_and_stores_nothing():
    service, groq, repo, whatsapp = build()
    groq.error = ExtractionError("all models failed")

    await service.handle(message(), None)

    assert whatsapp.texts == [ONBOARDING_ERROR_MESSAGE]
    assert repo.saved == []


async def test_storage_failure_does_not_pretend_the_data_was_saved():
    service, groq, repo, whatsapp = build()
    groq.onboarding_result = llm(full_name="Lucía Fernández", email="lucia@example.com")
    repo.fail_writes = True

    await service.handle(message("Lucía Fernández lucia@example.com"), None)

    assert whatsapp.texts == [ONBOARDING_ERROR_MESSAGE]
    assert PENDING_APPROVAL_MESSAGE not in whatsapp.texts
