"""Tests of the endpoint the Google Apps Script trigger calls when the `estado` column is edited."""

import logging
import pytest
from fastapi.testclient import TestClient
from src.api.v1.dependencies import get_access_notifier, get_settings, get_user_repository
from src.core.config import Settings
from src.domain.enums import UserStatusEnum
from src.domain.models import UserRecord
from src.main import app
from src.services.access_notifier import REJECTED_MESSAGE, AccessNotifier
from tests.fakes import FakeUserRepository, FakeWhatsApp

SECRET = "sheets-test-secret"
URL = "/api/v1/webhooks/sheets"
HEADERS = {"x-webhook-secret": SECRET}
PHONE = "5491122334455"


def make_settings(**overrides) -> Settings:
    values = {"SHEETS_WEBHOOK_SECRET": SECRET, "EVOLUTION_WEBHOOK_SECRET": "another-secret"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def approved(phone: str = PHONE, name: str = None) -> UserRecord:
    return UserRecord(phone=phone, status=UserStatusEnum.APPROVED, full_name=name)


def rejected(phone: str = PHONE) -> UserRecord:
    return UserRecord(phone=phone, status=UserStatusEnum.REJECTED)


@pytest.fixture
def api():
    users = FakeUserRepository()
    whatsapp = FakeWhatsApp()
    app.dependency_overrides[get_settings] = lambda: make_settings()
    app.dependency_overrides[get_user_repository] = lambda: users
    app.dependency_overrides[get_access_notifier] = lambda: AccessNotifier(users, whatsapp)
    yield TestClient(app), users, whatsapp
    app.dependency_overrides.clear()


def test_an_edit_invalidates_only_that_phone(api):
    client, users, _ = api

    response = client.post(URL, json={"event": "estado_changed", "sheet": "usuarios", "phone": "5491122334455"}, headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"status": "INVALIDATED"}
    assert users.invalidated == ["5491122334455"]
    assert users.invalidated_all == 0


def test_the_phone_is_normalized_to_digits(api):
    client, users, _ = api

    client.post(URL, json={"phone": "+54 9 11 2233-4455"}, headers=HEADERS)

    assert users.invalidated == ["5491122334455"]


def test_without_a_phone_the_whole_cache_is_invalidated(api):
    client, users, _ = api

    response = client.post(URL, json={"event": "bulk_edit", "sheet": "usuarios"}, headers=HEADERS)

    assert response.json() == {"status": "INVALIDATED_ALL"}
    assert users.invalidated_all == 1 and users.invalidated == []


def test_a_ping_checks_the_connection_without_touching_the_cache(api):
    client, users, whatsapp = api

    response = client.post(URL, json={"event": "ping"}, headers=HEADERS)

    assert response.json() == {"status": "PONG"}
    assert users.invalidated == [] and users.invalidated_all == 0
    assert whatsapp.sent == []


def test_edits_of_other_tabs_are_ignored(api):
    client, users, _ = api

    response = client.post(URL, json={"sheet": "raw_expenses", "phone": "5491122334455"}, headers=HEADERS)

    assert response.json() == {"status": "IGNORED"}
    assert users.invalidated == []


@pytest.mark.parametrize("phone", ["abc", "123", "1" * 30, ""])
def test_invalid_phones_are_rejected(api, phone):
    client, users, _ = api

    response = client.post(URL, json={"phone": phone}, headers=HEADERS)

    assert response.status_code == 422
    assert users.invalidated == []


@pytest.mark.parametrize("headers", [{}, {"x-webhook-secret": "wrong"}, {"x-webhook-secret": ""}, {"x-webhook-secret": SECRET + "x"}])
def test_calls_without_the_right_secret_are_rejected(api, headers):
    client, users, whatsapp = api

    response = client.post(URL, json={"phone": "5491122334455"}, headers=headers)

    assert response.status_code == 401
    assert users.invalidated == [] and users.invalidated_all == 0
    assert whatsapp.sent == []


def test_the_evolution_secret_does_not_open_the_sheets_endpoint(api):
    """Separate trust domains: leaking one secret must not grant the other capability."""
    client, users, _ = api

    response = client.post(URL, json={"phone": "5491122334455"}, headers={"x-webhook-secret": "another-secret"})

    assert response.status_code == 401
    assert users.invalidated == []


def test_fails_closed_when_the_secret_is_not_configured():
    users = FakeUserRepository()
    whatsapp = FakeWhatsApp()
    app.dependency_overrides[get_settings] = lambda: make_settings(SHEETS_WEBHOOK_SECRET="")
    app.dependency_overrides[get_user_repository] = lambda: users
    app.dependency_overrides[get_access_notifier] = lambda: AccessNotifier(users, whatsapp)
    try:
        client = TestClient(app)
        assert client.post(URL, json={"phone": "5491122334455"}, headers=HEADERS).status_code == 503
        assert client.post(URL, json={"phone": "5491122334455"}, headers={"x-webhook-secret": ""}).status_code == 503
        assert client.post(URL, json={"phone": "5491122334455"}).status_code == 503
        assert users.invalidated == [] and whatsapp.sent == []
    finally:
        app.dependency_overrides.clear()


def test_the_secret_and_the_full_phone_are_never_logged(api, caplog):
    client, _, _ = api

    with caplog.at_level(logging.DEBUG, logger="bot_wpp"):
        client.post(URL, json={"phone": "5491122334455"}, headers=HEADERS)
        client.post(URL, json={"phone": "5491122334455"}, headers={"x-webhook-secret": "wrong"})

    assert SECRET not in caplog.text and "5491122334455" not in caplog.text


def test_get_is_not_allowed(api):
    client, _, _ = api
    assert client.get(URL, headers=HEADERS).status_code == 405


@pytest.mark.parametrize("phone", ["abc", ""])
def test_invalid_phones_inside_a_batch_are_rejected(api, phone):
    client, users, _ = api

    response = client.post(URL, json={"changes": [{"phone": PHONE}, {"phone": phone}]}, headers=HEADERS)

    assert response.status_code == 422
    assert users.invalidated == [] and users.invalidated_all == 0


def test_an_approval_is_announced_to_the_user(api):
    client, users, whatsapp = api
    users.users[PHONE] = approved(name="Ana Gómez")

    response = client.post(URL, json={"sheet": "usuarios", "phone": PHONE, "estado": 1}, headers=HEADERS)

    assert response.json() == {"status": "INVALIDATED"}
    assert [phone for phone, _ in whatsapp.sent] == [PHONE]
    assert "¡Hola, Ana!" in whatsapp.texts[0] and "ya fue aprobada" in whatsapp.texts[0]


def test_a_rejection_is_announced_to_the_user(api):
    client, users, whatsapp = api
    users.users[PHONE] = rejected()

    client.post(URL, json={"phone": PHONE, "estado": 2, "estado_anterior": 1}, headers=HEADERS)

    assert whatsapp.texts == [REJECTED_MESSAGE]


def test_a_pending_user_is_not_announced(api):
    client, users, whatsapp = api
    users.users[PHONE] = UserRecord(phone=PHONE, status=None, full_name="Ana")

    client.post(URL, json={"phone": PHONE, "estado": ""}, headers=HEADERS)

    assert whatsapp.sent == []


def test_a_forged_approval_does_not_message_a_number_the_sheet_does_not_approve(api):
    """The secret only allows asking the bot to re-read the sheet: never to message an arbitrary number."""
    client, users, whatsapp = api
    users.users[PHONE] = UserRecord(phone=PHONE, status=None, full_name="Ana")

    client.post(URL, json={"phone": PHONE, "estado": 1}, headers=HEADERS)

    assert whatsapp.sent == []


def test_a_re_saved_value_is_not_announced_again(api):
    client, users, whatsapp = api
    users.users[PHONE] = approved()

    client.post(URL, json={"phone": PHONE, "estado": 1, "estado_anterior": 1}, headers=HEADERS)

    assert whatsapp.sent == []


def test_a_batch_refreshes_the_whole_cache_and_announces_each_row(api):
    client, users, whatsapp = api
    second = "5491122334466"
    users.users[PHONE] = approved()
    users.users[second] = rejected(second)

    response = client.post(
        URL,
        json={"sheet": "usuarios", "changes": [{"phone": PHONE, "estado": 1}, {"phone": second, "estado": 2}]},
        headers=HEADERS,
    )

    assert response.json() == {"status": "INVALIDATED"}
    assert users.invalidated_all == 1  # one whole-table refresh serves every row of the batch
    assert users.invalidated == []
    assert [phone for phone, _ in whatsapp.sent] == [PHONE, second]


def test_an_empty_batch_only_invalidates_everything(api):
    client, users, whatsapp = api
    users.users[PHONE] = approved()

    client.post(URL, json={"changes": []}, headers=HEADERS)

    assert users.invalidated_all == 1
    assert whatsapp.sent == []


def test_a_legacy_payload_without_estado_still_announces_the_sheet_state(api):
    client, users, whatsapp = api
    users.users[PHONE] = approved()

    client.post(URL, json={"event": "estado_changed", "sheet": "usuarios", "phone": PHONE}, headers=HEADERS)

    assert len(whatsapp.sent) == 1


def test_an_undeliverable_notice_does_not_break_the_webhook(api):
    client, users, whatsapp = api
    users.users[PHONE] = approved()
    whatsapp.fail_sends = True

    response = client.post(URL, json={"phone": PHONE, "estado": 1}, headers=HEADERS)

    assert response.status_code == 200 and response.json() == {"status": "INVALIDATED"}
    assert whatsapp.sent == []


def test_a_failing_sheet_read_never_messages_the_user(api):
    client, users, whatsapp = api
    users.users[PHONE] = approved()
    users.fail_reads = True

    response = client.post(URL, json={"phone": PHONE, "estado": 1}, headers=HEADERS)

    assert response.status_code == 200
    assert whatsapp.sent == []
