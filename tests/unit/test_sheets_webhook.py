"""Tests of the endpoint the Google Apps Script trigger calls when the `estado` column is edited."""

import logging
import pytest
from fastapi.testclient import TestClient
from src.api.v1.dependencies import get_settings, get_user_repository
from src.core.config import Settings
from src.main import app
from tests.fakes import FakeUserRepository

SECRET = "sheets-test-secret"
URL = "/api/v1/webhooks/sheets"
HEADERS = {"x-webhook-secret": SECRET}


def make_settings(**overrides) -> Settings:
    values = {"SHEETS_WEBHOOK_SECRET": SECRET, "EVOLUTION_WEBHOOK_SECRET": "another-secret"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def api():
    users = FakeUserRepository()
    app.dependency_overrides[get_settings] = lambda: make_settings()
    app.dependency_overrides[get_user_repository] = lambda: users
    yield TestClient(app), users
    app.dependency_overrides.clear()


def test_an_edit_invalidates_only_that_phone(api):
    client, users = api

    response = client.post(URL, json={"event": "estado_changed", "sheet": "usuarios", "phone": "5491122334455"}, headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"status": "INVALIDATED"}
    assert users.invalidated == ["5491122334455"]
    assert users.invalidated_all == 0


def test_the_phone_is_normalized_to_digits(api):
    client, users = api

    client.post(URL, json={"phone": "+54 9 11 2233-4455"}, headers=HEADERS)

    assert users.invalidated == ["5491122334455"]


def test_without_a_phone_the_whole_cache_is_invalidated(api):
    client, users = api

    response = client.post(URL, json={"event": "bulk_edit", "sheet": "usuarios"}, headers=HEADERS)

    assert response.json() == {"status": "INVALIDATED_ALL"}
    assert users.invalidated_all == 1 and users.invalidated == []


def test_a_ping_checks_the_connection_without_touching_the_cache(api):
    client, users = api

    response = client.post(URL, json={"event": "ping"}, headers=HEADERS)

    assert response.json() == {"status": "PONG"}
    assert users.invalidated == [] and users.invalidated_all == 0


def test_edits_of_other_tabs_are_ignored(api):
    client, users = api

    response = client.post(URL, json={"sheet": "raw_expenses", "phone": "5491122334455"}, headers=HEADERS)

    assert response.json() == {"status": "IGNORED"}
    assert users.invalidated == []


@pytest.mark.parametrize("phone", ["abc", "123", "1" * 30, ""])
def test_invalid_phones_are_rejected(api, phone):
    client, users = api

    response = client.post(URL, json={"phone": phone}, headers=HEADERS)

    assert response.status_code == 422
    assert users.invalidated == []


@pytest.mark.parametrize("headers", [{}, {"x-webhook-secret": "wrong"}, {"x-webhook-secret": ""}, {"x-webhook-secret": SECRET + "x"}])
def test_calls_without_the_right_secret_are_rejected(api, headers):
    client, users = api

    response = client.post(URL, json={"phone": "5491122334455"}, headers=headers)

    assert response.status_code == 401
    assert users.invalidated == [] and users.invalidated_all == 0


def test_the_evolution_secret_does_not_open_the_sheets_endpoint(api):
    """Separate trust domains: leaking one secret must not grant the other capability."""
    client, users = api

    response = client.post(URL, json={"phone": "5491122334455"}, headers={"x-webhook-secret": "another-secret"})

    assert response.status_code == 401
    assert users.invalidated == []


def test_fails_closed_when_the_secret_is_not_configured():
    users = FakeUserRepository()
    app.dependency_overrides[get_settings] = lambda: make_settings(SHEETS_WEBHOOK_SECRET="")
    app.dependency_overrides[get_user_repository] = lambda: users
    try:
        client = TestClient(app)
        assert client.post(URL, json={"phone": "5491122334455"}, headers=HEADERS).status_code == 503
        assert client.post(URL, json={"phone": "5491122334455"}, headers={"x-webhook-secret": ""}).status_code == 503
        assert client.post(URL, json={"phone": "5491122334455"}).status_code == 503
        assert users.invalidated == []
    finally:
        app.dependency_overrides.clear()


def test_the_secret_and_the_full_phone_are_never_logged(api, caplog):
    client, _ = api

    with caplog.at_level(logging.DEBUG, logger="bot_wpp"):
        client.post(URL, json={"phone": "5491122334455"}, headers=HEADERS)
        client.post(URL, json={"phone": "5491122334455"}, headers={"x-webhook-secret": "wrong"})

    assert SECRET not in caplog.text and "5491122334455" not in caplog.text


def test_get_is_not_allowed(api):
    client, _ = api
    assert client.get(URL, headers=HEADERS).status_code == 405
