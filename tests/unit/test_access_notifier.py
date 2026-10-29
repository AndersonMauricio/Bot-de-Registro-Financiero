"""Unit tests of the WhatsApp notice an administrator's edit triggers (approval / rejection)."""

import logging
import pytest
from src.domain.enums import UserStatusEnum
from src.domain.models import UserRecord
from src.infrastructure.cache.ttl_cache import TTLCache
from src.infrastructure.storage.sheets_users_repo import HEADERS, GoogleSheetsUserRepository
from src.services.access_notifier import (
    NOTICE_TTL_SECONDS,
    REJECTED_MESSAGE,
    AccessChange,
    AccessNotifier,
)
from tests.fakes import FakeClock, FakeUserRepository, FakeWhatsApp, FakeWorksheet

pytestmark = pytest.mark.asyncio

PHONE = "5491100000001"


def user(status=None, name=None) -> UserRecord:
    return UserRecord(phone=PHONE, status=status, full_name=name)


def build(users: dict = None, clock: FakeClock = None):
    repo = FakeUserRepository(users or {})
    whatsapp = FakeWhatsApp()
    notified = TTLCache(NOTICE_TTL_SECONDS, clock=clock or FakeClock())
    return AccessNotifier(repo, whatsapp, notified=notified), repo, whatsapp


async def test_an_approved_user_is_told_they_can_register_expenses():
    notifier, _, whatsapp = build({PHONE: user(UserStatusEnum.APPROVED)})

    await notifier.notify([AccessChange.build(PHONE, estado=1, estado_anterior="")])

    assert len(whatsapp.sent) == 1
    sent_phone, text = whatsapp.sent[0]
    assert sent_phone == PHONE
    assert "ya fue aprobada" in text and "Gasté 5000" in text


async def test_the_notice_greets_the_user_by_first_name():
    notifier, _, whatsapp = build({PHONE: user(UserStatusEnum.APPROVED, "Ana María Gómez")})

    await notifier.notify([AccessChange.build(PHONE, estado="1")])

    assert whatsapp.texts[0].startswith("✅ ¡Hola, Ana!")


async def test_without_a_name_the_notice_has_no_greeting():
    notifier, _, whatsapp = build({PHONE: user(UserStatusEnum.APPROVED)})

    await notifier.notify([AccessChange.build(PHONE, estado=1)])

    assert whatsapp.texts[0].startswith("✅ ¡Hola! Tu cuenta ya fue aprobada.")


async def test_a_rejected_user_is_told_the_request_was_not_approved():
    notifier, _, whatsapp = build({PHONE: user(UserStatusEnum.REJECTED)})

    await notifier.notify([AccessChange.build(PHONE, estado=2, estado_anterior=1)])

    assert whatsapp.texts == [REJECTED_MESSAGE]


async def test_a_pending_user_is_told_nothing():
    notifier, _, whatsapp = build({PHONE: user(None)})

    await notifier.notify([AccessChange.build(PHONE, estado="")])

    assert whatsapp.sent == []


async def test_the_notice_follows_the_sheet_and_never_the_payload():
    """A forged 'estado: 1' must not message a number the sheet does not have as approved."""
    notifier, _, whatsapp = build({PHONE: user(None)})

    await notifier.notify([AccessChange.build(PHONE, estado=1)])

    assert whatsapp.sent == []


async def test_a_payload_without_estado_still_announces_the_sheet_state():
    notifier, _, whatsapp = build({PHONE: user(UserStatusEnum.APPROVED)})

    await notifier.notify([AccessChange.build(PHONE)])

    assert len(whatsapp.sent) == 1


async def test_saving_the_same_value_again_does_not_repeat_the_notice():
    notifier, _, whatsapp = build({PHONE: user(UserStatusEnum.APPROVED)})

    await notifier.notify([AccessChange.build(PHONE, estado=1, estado_anterior=1)])

    assert whatsapp.sent == []


async def test_the_same_state_is_not_announced_twice():
    notifier, _, whatsapp = build({PHONE: user(UserStatusEnum.APPROVED)})

    await notifier.notify([AccessChange.build(PHONE, estado=1)])
    await notifier.notify([AccessChange.build(PHONE, estado=1)])

    assert len(whatsapp.sent) == 1


async def test_a_state_is_announced_again_after_the_dedupe_window():
    clock = FakeClock()
    notifier, _, whatsapp = build({PHONE: user(UserStatusEnum.APPROVED)}, clock=clock)

    await notifier.notify([AccessChange.build(PHONE, estado=1)])
    clock.advance(NOTICE_TTL_SECONDS + 1)
    await notifier.notify([AccessChange.build(PHONE, estado=1)])

    assert len(whatsapp.sent) == 2


async def test_a_failed_send_is_not_remembered_as_sent():
    notifier, _, whatsapp = build({PHONE: user(UserStatusEnum.APPROVED)})
    whatsapp.fail_sends = True

    await notifier.notify([AccessChange.build(PHONE, estado=1)])
    whatsapp.fail_sends = False
    await notifier.notify([AccessChange.build(PHONE, estado=1)])

    assert len(whatsapp.sent) == 1


async def test_a_failing_sheet_read_sends_nothing_and_does_not_raise():
    notifier, repo, whatsapp = build({PHONE: user(UserStatusEnum.APPROVED)})
    repo.fail_reads = True

    await notifier.notify([AccessChange.build(PHONE, estado=1)])

    assert whatsapp.sent == []


async def test_a_send_error_does_not_escape_the_background_task():
    notifier, _, whatsapp = build({PHONE: user(UserStatusEnum.APPROVED)})

    async def boom(*args, **kwargs):
        raise RuntimeError("gateway down")

    whatsapp.send_text_message = boom

    await notifier.notify([AccessChange.build(PHONE, estado=1)])  # must not raise


async def test_a_batch_is_announced_once_per_phone():
    other = "5491100000002"
    notifier, _, whatsapp = build(
        {PHONE: user(UserStatusEnum.APPROVED), other: user(UserStatusEnum.REJECTED)}
    )

    await notifier.notify([
        AccessChange.build(PHONE, estado=1),
        AccessChange.build(other, estado=2),
        AccessChange.build(PHONE, estado=1),
    ])

    assert sorted(phone for phone, _ in whatsapp.sent) == [PHONE, other]


async def test_the_full_phone_is_never_logged(caplog):
    notifier, _, _ = build({PHONE: user(UserStatusEnum.APPROVED)})

    with caplog.at_level(logging.DEBUG, logger="bot_wpp"):
        await notifier.notify([AccessChange.build(PHONE, estado=1)])

    assert PHONE not in caplog.text


async def test_a_batch_of_notices_costs_a_single_sheet_read():
    """A pasted range of approvals must not spend one Sheets request per row."""
    second, third = "5491100000002", "5491100000003"
    ws = FakeWorksheet([
        HEADERS,
        [PHONE, "Ana Prueba", "", 1, ""],
        [second, "Beto Prueba", "", 1, ""],
        [third, "Carla Prueba", "", 2, ""],
    ])
    repo = GoogleSheetsUserRepository(worksheet=ws, clock=FakeClock())
    whatsapp = FakeWhatsApp()

    repo.invalidate_all()  # what the endpoint does for an edit that touched several rows
    await AccessNotifier(repo, whatsapp).notify([
        AccessChange.build(PHONE, estado=1),
        AccessChange.build(second, estado=1),
        AccessChange.build(third, estado=2),
    ])

    assert len(whatsapp.sent) == 3
    assert ws.reads == 1  # the first notice refreshes the table for the whole batch
    assert repo.get_user(PHONE).status == UserStatusEnum.APPROVED
    assert ws.reads == 1  # and the next message from that user is served from memory
