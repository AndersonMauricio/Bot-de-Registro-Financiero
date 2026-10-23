"""Unit tests for the three-state access router."""

import asyncio
import pytest
from src.domain.enums import InputTypeEnum, UserStatusEnum
from src.domain.models import UserRecord
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services.message_router import (
    ACCESS_CHECK_UNAVAILABLE_MESSAGE,
    ACCESS_DENIED_MESSAGE,
    UNEXPECTED_ERROR_MESSAGE,
    MessageRouter,
)
from tests.fakes import FakeUserRepository, FakeWhatsApp

pytestmark = pytest.mark.asyncio

PHONE = "5491100000001"


def message(message_id: str = "msg-1", phone: str = PHONE, text: str = "gasté 500 en uber") -> ParsedWhatsAppMessage:
    return ParsedWhatsAppMessage(
        message_id=message_id,
        sender_phone=phone,
        timestamp="1729382400",
        input_type=InputTypeEnum.TEXT,
        text_content=text,
    )


class RecordingFlow:
    """Stands in for the expense and onboarding services."""

    def __init__(self, delay: float = 0.0, error: Exception = None):
        self.calls: list = []
        self.delay = delay
        self.error = error
        self.running = 0
        self.max_running = 0

    async def process_incoming_message(self, parsed_msg):
        await self._record(parsed_msg)

    async def handle(self, parsed_msg, user):
        await self._record(parsed_msg, user)

    async def _record(self, *args):
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            self.calls.append(args)
            await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
        finally:
            self.running -= 1


def build(users: dict = None, expense=None, onboarding=None):
    repo = FakeUserRepository(users)
    whatsapp = FakeWhatsApp()
    expense = expense or RecordingFlow()
    onboarding = onboarding or RecordingFlow()
    router = MessageRouter(repo, expense, onboarding, whatsapp)
    return router, repo, whatsapp, expense, onboarding


def user(status, phone: str = PHONE) -> UserRecord:
    return UserRecord(phone=phone, status=status)


async def test_state_1_approved_goes_straight_to_expense_registration():
    router, _, whatsapp, expense, onboarding = build({PHONE: user(UserStatusEnum.APPROVED)})
    msg = message()

    await router.handle(msg)

    assert expense.calls == [(msg,)]
    assert onboarding.calls == []
    assert whatsapp.sent == []  # the router itself says nothing: the expense flow replies


async def test_state_2_rejected_gets_the_standard_denial_and_the_flow_stops():
    router, _, whatsapp, expense, onboarding = build({PHONE: user(UserStatusEnum.REJECTED)})

    await router.handle(message())

    assert whatsapp.sent == [(PHONE, ACCESS_DENIED_MESSAGE)]
    assert expense.calls == []
    assert onboarding.calls == []
    assert ("typing_start", PHONE) not in whatsapp.events  # no work is spent on rejected users


async def test_state_null_unknown_number_starts_onboarding():
    router, _, whatsapp, expense, onboarding = build({})
    msg = message()

    await router.handle(msg)

    assert onboarding.calls == [(msg, None)]
    assert expense.calls == []
    assert whatsapp.sent == []


async def test_state_null_registered_but_not_yet_approved_continues_onboarding():
    pending = user(None)
    router, _, _, expense, onboarding = build({PHONE: pending})
    msg = message()

    await router.handle(msg)

    assert onboarding.calls == [(msg, pending)]
    assert expense.calls == []


async def test_if_the_state_cannot_be_verified_nothing_is_registered():
    """Strict validation fails closed: no sheet, no expenses (and no onboarding either)."""
    router, repo, whatsapp, expense, onboarding = build({PHONE: user(UserStatusEnum.APPROVED)})
    repo.fail_reads = True

    await router.handle(message())

    assert whatsapp.sent == [(PHONE, ACCESS_CHECK_UNAVAILABLE_MESSAGE)]
    assert expense.calls == []
    assert onboarding.calls == []


async def test_a_redelivered_webhook_is_processed_only_once():
    router, repo, _, expense, _ = build({PHONE: user(UserStatusEnum.APPROVED)})

    await router.handle(message("same-id"))
    await router.handle(message("same-id"))
    await router.handle(message("other-id"))

    assert [call[0].message_id for call in expense.calls] == ["same-id", "other-id"]
    assert repo.get_calls == 2


async def test_messages_from_the_same_sender_are_processed_one_at_a_time():
    """Two quick messages from a new user must not both create the onboarding row."""
    onboarding = RecordingFlow(delay=0.05)
    router, _, _, _, _ = build({}, onboarding=onboarding)

    await asyncio.gather(
        router.handle(message("m1")),
        router.handle(message("m2")),
        router.handle(message("m3")),
    )

    assert len(onboarding.calls) == 3
    assert onboarding.max_running == 1


async def test_different_senders_are_processed_concurrently():
    other = "5491100000002"
    onboarding = RecordingFlow(delay=0.05)
    router, _, _, _, _ = build({}, onboarding=onboarding)

    await asyncio.gather(router.handle(message("m1", PHONE)), router.handle(message("m2", other)))

    assert onboarding.max_running == 2


async def test_an_unexpected_failure_is_reported_to_the_user_and_never_raised():
    expense = RecordingFlow(error=RuntimeError("boom"))
    router, _, whatsapp, _, _ = build({PHONE: user(UserStatusEnum.APPROVED)}, expense=expense)

    await router.handle(message())  # must not raise: it runs as a background task

    assert whatsapp.sent == [(PHONE, UNEXPECTED_ERROR_MESSAGE)]


async def test_lock_registry_does_not_grow_with_every_sender():
    router, _, _, _, _ = build({})

    for i in range(20):
        await router.handle(message(f"m{i}", f"54911000000{i:02d}"))

    assert len(router._locks) == 0
