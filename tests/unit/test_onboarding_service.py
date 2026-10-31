"""Unit tests for the onboarding flow of unknown users."""

import pytest
from src.core.exceptions import ExtractionError
from src.domain.enums import InputTypeEnum
from src.domain.models import OnboardingData, OnboardingLLMResponse, UserRecord
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services.onboarding_service import (
    ONBOARDING_ERROR_MESSAGE,
    ONBOARDING_MISSING_EMAIL_MESSAGE,
    ONBOARDING_MISSING_NAME_MESSAGE,
    ONBOARDING_NEW_MESSAGE,
    PENDING_APPROVAL_MESSAGE,
    PENDING_REMINDER_MESSAGE,
    OnboardingService,
    onboarding_reply,
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


def llm(**collected) -> OnboardingLLMResponse:
    """What the extraction LLM found in the message (the LLM never writes the reply)."""
    return OnboardingLLMResponse(collected=OnboardingData(**collected))


def data_saves(repo: FakeUserRepository) -> list:
    """Saves that carry data (the empty first-contact registration is checked separately)."""
    return [(phone, data) for phone, data in repo.saved if data != OnboardingData()]


def build(users: dict = None):
    groq, repo, whatsapp = FakeGroq(), FakeUserRepository(users), FakeWhatsApp()
    return OnboardingService(groq, repo, whatsapp), groq, repo, whatsapp


async def test_first_contact_without_data_gets_the_welcome_text_and_stores_nothing_yet():
    service, groq, repo, whatsapp = build()
    groq.onboarding_result = llm()

    await service.handle(message("hola, gasté 500 en uber"), None)

    assert whatsapp.texts == [ONBOARDING_NEW_MESSAGE]
    assert data_saves(repo) == []  # nothing was collected yet
    assert groq.expense_calls == []  # never touches the expense extraction branch


async def test_only_the_name_is_saved_and_the_email_is_asked():
    service, groq, repo, whatsapp = build()
    groq.onboarding_result = llm(full_name="Lucía Fernández")

    await service.handle(message("me llamo Lucía Fernández"), None)

    assert data_saves(repo) == [(PHONE, OnboardingData(full_name="Lucía Fernández"))]
    assert whatsapp.texts == [ONBOARDING_MISSING_EMAIL_MESSAGE]


async def test_only_the_email_is_saved_and_the_name_is_asked():
    service, groq, repo, whatsapp = build()
    groq.onboarding_result = llm(email="lucia@example.com")

    await service.handle(message("mi mail es lucia@example.com"), None)

    assert data_saves(repo) == [(PHONE, OnboardingData(email="lucia@example.com"))]
    assert whatsapp.texts == [ONBOARDING_MISSING_NAME_MESSAGE]


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
    # Every reply is fixed copy: nothing the model writes can announce an approval
    groq.onboarding_result = llm(email="lucia@example.com")

    await service.handle(message("lucia@example.com"), existing)

    assert data_saves(repo) == [(PHONE, OnboardingData(full_name="Lucía Fernández", email="lucia@example.com"))]
    assert whatsapp.texts == [PENDING_APPROVAL_MESSAGE]
    assert "aprobación manual del administrador" in PENDING_APPROVAL_MESSAGE
    assert "antes de poder registrar gastos" in PENDING_APPROVAL_MESSAGE


async def test_all_data_in_a_single_message_completes_the_onboarding():
    service, groq, repo, whatsapp = build()
    groq.onboarding_result = llm(full_name="Lucía Fernández", email="lucia@example.com")

    await service.handle(message("Soy Lucía Fernández, lucia@example.com"), None)

    assert len(data_saves(repo)) == 1
    assert whatsapp.texts == [PENDING_APPROVAL_MESSAGE]


async def test_a_user_that_already_finished_waits_for_the_admin_without_any_llm_call():
    done = UserRecord(phone=PHONE, full_name="Lucía Fernández", email="lucia@example.com")
    service, groq, repo, whatsapp = build({PHONE: done})

    await service.handle(message("¿ya estoy aprobada?"), done)

    assert whatsapp.texts == [PENDING_REMINDER_MESSAGE]
    assert groq.onboarding_calls == []
    assert data_saves(repo) == []
    assert ("typing_start", PHONE) not in whatsapp.events


@pytest.mark.parametrize("input_type", [InputTypeEnum.AUDIO, InputTypeEnum.IMAGE])
async def test_onboarding_is_text_only_so_no_media_is_processed_for_unapproved_users(input_type):
    service, groq, repo, whatsapp = build()

    await service.handle(message("", input_type), None)

    assert "por mensaje de texto" in whatsapp.texts[0]
    assert groq.onboarding_calls == []
    assert data_saves(repo) == []


async def test_an_email_the_user_never_wrote_is_discarded():
    service, groq, repo, whatsapp = build()
    groq.onboarding_result = llm(full_name="Lucía Fernández", email="invented@example.com")

    await service.handle(message("soy Lucía Fernández"), None)

    assert data_saves(repo) == [(PHONE, OnboardingData(full_name="Lucía Fernández"))]
    assert whatsapp.texts == [ONBOARDING_MISSING_EMAIL_MESSAGE]


async def test_user_provided_text_is_neutralized_against_formula_injection():
    service, groq, repo, _ = build()
    groq.onboarding_result = llm(full_name='=HYPERLINK("http://evil.example","click")', email="x@example.com")

    await service.handle(message('=HYPERLINK("http://evil.example","click") x@example.com'), None)

    saved = data_saves(repo)[0][1]
    assert saved.full_name.startswith("'=")


async def test_typing_covers_the_llm_work_and_ends_before_the_reply():
    service, groq, _, whatsapp = build()
    groq.onboarding_result = llm()

    await service.handle(message(), None)

    assert [e[0] for e in whatsapp.events] == ["typing_start", "typing_end", "send"]


async def test_a_message_without_new_data_repeats_what_is_still_missing():
    """The reply follows what is stored, not what this message brought."""
    name_only = UserRecord(phone=PHONE, full_name="Lucía Fernández")
    email_only = UserRecord(phone=PHONE, email="lucia@example.com")

    for existing, expected in ((name_only, ONBOARDING_MISSING_EMAIL_MESSAGE), (email_only, ONBOARDING_MISSING_NAME_MESSAGE)):
        service, groq, repo, whatsapp = build({PHONE: existing})
        groq.onboarding_result = llm()

        await service.handle(message("ahí te lo paso"), existing)

        assert whatsapp.texts == [expected]
        assert data_saves(repo) == []  # nothing new, nothing written


def test_the_onboarding_fsm_texts_are_the_agreed_copy():
    assert ONBOARDING_NEW_MESSAGE == (
        "¡Hola! Para darte de alta y registrar tus gastos, necesito tu nombre, apellido y un email "
        "(preferentemente de Gmail)."
    )
    assert ONBOARDING_MISSING_EMAIL_MESSAGE == "Tengo tu nombre, pero me falta tu email. ¿Me lo pasás?"
    assert ONBOARDING_MISSING_NAME_MESSAGE == "Tengo tu email, pero me falta tu nombre y apellido."


@pytest.mark.parametrize(
    "data, expected",
    [
        (OnboardingData(), ONBOARDING_NEW_MESSAGE),
        (OnboardingData(full_name="Lucía Fernández"), ONBOARDING_MISSING_EMAIL_MESSAGE),
        (OnboardingData(email="lucia@example.com"), ONBOARDING_MISSING_NAME_MESSAGE),
        (OnboardingData(full_name="Lucía Fernández", email="lucia@example.com"), PENDING_APPROVAL_MESSAGE),
    ],
)
def test_the_onboarding_fsm_picks_the_reply_from_what_is_missing(data, expected):
    assert onboarding_reply(data) == expected


async def test_llm_failure_tells_the_user_to_retry_and_stores_nothing():
    service, groq, repo, whatsapp = build()
    groq.error = ExtractionError("all models failed")

    await service.handle(message(), None)

    assert whatsapp.texts == [ONBOARDING_ERROR_MESSAGE]
    assert data_saves(repo) == []


async def test_storage_failure_does_not_pretend_the_data_was_saved():
    service, groq, repo, whatsapp = build()
    groq.onboarding_result = llm(full_name="Lucía Fernández", email="lucia@example.com")
    repo.fail_writes = True

    await service.handle(message("Lucía Fernández lucia@example.com"), None)

    assert whatsapp.texts == [ONBOARDING_ERROR_MESSAGE]
    assert PENDING_APPROVAL_MESSAGE not in whatsapp.texts


# ------------------------------------------------- first contact registers the number
async def test_a_new_number_is_registered_in_the_users_sheet_at_first_contact_even_without_data():
    service, groq, repo, whatsapp = build()
    groq.onboarding_result = llm()

    await service.handle(message("hola"), None)

    assert repo.saved[0] == (PHONE, OnboardingData())  # row created: the admin can see who wrote
    assert PHONE in repo.users and repo.users[PHONE].status is None


async def test_a_number_already_registered_is_not_registered_again():
    existing = UserRecord(phone=PHONE, full_name="Lucía Fernández")
    service, groq, repo, _ = build({PHONE: existing})

    await service.handle(message("mi mail es lucia@example.com"), existing)

    assert OnboardingData() not in [data for _, data in repo.saved]


@pytest.mark.parametrize("input_type", [InputTypeEnum.AUDIO, InputTypeEnum.IMAGE])
async def test_a_new_number_is_registered_even_if_its_first_message_is_not_text(input_type):
    service, _, repo, _ = build()

    await service.handle(message("", input_type), None)

    assert repo.saved == [(PHONE, OnboardingData())]


async def test_if_the_number_cannot_be_registered_the_user_is_asked_to_retry_and_the_llm_is_not_called():
    service, groq, repo, whatsapp = build()
    repo.fail_writes = True

    await service.handle(message("hola"), None)

    assert whatsapp.texts == [ONBOARDING_ERROR_MESSAGE]
    assert groq.onboarding_calls == []
