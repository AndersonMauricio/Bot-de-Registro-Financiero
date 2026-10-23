"""Unit tests for the on-demand report: strict per-user isolation, read-only sharing and abuse protection."""

import pytest
from datetime import date
from src.domain.enums import InputTypeEnum
from src.domain.models import ExpenseRecord, UserRecord
from src.infrastructure.cache.ttl_cache import TTLCache
from src.services.data_quality import DataQualityGate
from src.services.report_service import (
    COOLDOWN_MESSAGE,
    NO_EMAIL_MESSAGE,
    NO_EXPENSES_MESSAGE,
    REPORT_ERROR_MESSAGE,
    ReportService,
)
from tests.fakes import FakeClock, FakeExpenseRepository, FakeReportPublisher, FakeUserRepository

pytestmark = pytest.mark.asyncio

PHONE = "5491100000001"
OTHER_PHONE = "5491100000002"
EMAIL = "lucia.fernandez@example.com"


def expense(phone: str, amount: float, currency: str = "ARS", message_id: str = None) -> ExpenseRecord:
    return ExpenseRecord(
        message_id=message_id or f"m-{phone}-{amount}",
        user_phone_hash=DataQualityGate.hash_phone_number(phone),
        input_type=InputTypeEnum.TEXT,
        transaction_date=date(2026, 10, 19),
        amount=amount,
        currency=currency,
        category="Alimentación",
        subcategory="Supermercado",
        payment_method="Efectivo",
        description=f"gasto de {phone}",
        confidence_score=0.9,
        raw_text="x",
    )


def build(expenses=(), user_email=EMAIL, clock=None, ttl=300):
    repo = FakeExpenseRepository()
    for e in expenses:
        repo.saved.append(e)
    users = FakeUserRepository({PHONE: UserRecord(phone=PHONE, full_name="Lucía", email=user_email)})
    publisher = FakeReportPublisher()
    service = ReportService(repo, users, publisher, cooldown=TTLCache(ttl, clock=clock or FakeClock()))
    return service, repo, users, publisher


async def test_the_report_is_published_for_the_requester_and_shared_read_only_with_their_registered_email():
    service, repo, _, publisher = build([expense(PHONE, 1000), expense(PHONE, 250.5)])

    reply = await service.handle(PHONE)

    assert len(publisher.calls) == 1
    title, rows, email = publisher.calls[0]
    assert email == EMAIL
    assert [r["monto"] for r in rows] == [1000, 250.5]
    assert publisher.url in reply
    assert "2" in reply and "ARS 1,250.50" in reply
    assert "solo lectura" in reply
    assert EMAIL not in reply  # the address is masked in the chat
    assert "l***@example.com" in reply


async def test_only_the_requesters_own_expenses_are_ever_queried_and_published():
    """Strict isolation: another user's rows can never reach the requester's report."""
    service, repo, _, publisher = build([expense(PHONE, 100), expense(OTHER_PHONE, 999999)])

    await service.handle(PHONE)

    assert repo.queried_hashes == [DataQualityGate.hash_phone_number(PHONE)]
    assert all("999999" not in str(r) for r in publisher.calls[0][1])
    assert [r["descripcion"] for r in publisher.calls[0][1]] == [f"gasto de {PHONE}"]


async def test_the_report_title_does_not_contain_the_phone_number_or_the_email():
    service, _, _, publisher = build([expense(PHONE, 100)])

    await service.handle(PHONE)

    title = publisher.calls[0][0]
    assert PHONE not in title and EMAIL not in title and "Lucía" not in title
    assert DataQualityGate.hash_phone_number(PHONE)[:8] in title


async def test_totals_are_grouped_by_currency():
    service, _, _, _ = build([expense(PHONE, 1000), expense(PHONE, 15.5, "USD"), expense(PHONE, 500)])

    reply = await service.handle(PHONE)

    assert "ARS 1,500.00" in reply and "USD 15.50" in reply


@pytest.mark.parametrize("email", [None, ""])
async def test_without_a_registered_email_nothing_is_generated(email):
    service, repo, _, publisher = build([expense(PHONE, 100)], user_email=email)

    assert await service.handle(PHONE) == NO_EMAIL_MESSAGE

    assert publisher.calls == [] and repo.queried_hashes == []


async def test_a_user_without_expenses_gets_a_message_and_no_file():
    service, _, _, publisher = build([expense(OTHER_PHONE, 100)])

    assert await service.handle(PHONE) == NO_EXPENSES_MESSAGE

    assert publisher.calls == []


async def test_asking_twice_in_a_row_creates_a_single_file():
    service, _, _, publisher = build([expense(PHONE, 100)])

    first = await service.handle(PHONE)
    second = await service.handle(PHONE)

    assert publisher.url in first
    assert second == COOLDOWN_MESSAGE
    assert len(publisher.calls) == 1


async def test_a_new_report_is_allowed_once_the_cooldown_passes():
    clock = FakeClock()
    service, _, _, publisher = build([expense(PHONE, 100)], clock=clock, ttl=300)

    await service.handle(PHONE)
    clock.advance(301)
    await service.handle(PHONE)

    assert len(publisher.calls) == 2


async def test_a_failure_to_publish_tells_the_user_and_lets_them_retry_immediately():
    service, _, _, publisher = build([expense(PHONE, 100)])
    publisher.error = RuntimeError("drive quota")

    assert await service.handle(PHONE) == REPORT_ERROR_MESSAGE

    publisher.error = None
    reply = await service.handle(PHONE)  # no cooldown was consumed by the failure
    assert publisher.url in reply


async def test_an_empty_report_does_not_consume_the_cooldown():
    service, repo, _, publisher = build([])

    assert await service.handle(PHONE) == NO_EXPENSES_MESSAGE
    repo.saved.append(expense(PHONE, 100))

    assert publisher.url in await service.handle(PHONE)


async def test_the_users_cooldowns_are_independent():
    users = FakeUserRepository({
        PHONE: UserRecord(phone=PHONE, email=EMAIL),
        OTHER_PHONE: UserRecord(phone=OTHER_PHONE, email="otra@example.com"),
    })
    repo = FakeExpenseRepository()
    repo.saved += [expense(PHONE, 1), expense(OTHER_PHONE, 2)]
    publisher = FakeReportPublisher()
    service = ReportService(repo, users, publisher, cooldown=TTLCache(300, clock=FakeClock()))

    await service.handle(PHONE)
    await service.handle(OTHER_PHONE)

    assert [call[2] for call in publisher.calls] == [EMAIL, "otra@example.com"]
