"""Integration-style tests of the FastAPI webhook layer (no external services)."""

import logging
import pytest
from fastapi.testclient import TestClient
from src.api.v1.dependencies import get_message_router, get_settings
from src.core.config import Settings
from src.main import app

SECRET = "test-webhook-secret"
URL = "/api/v1/webhooks/evolution"
HEADERS = {"x-webhook-secret": SECRET}


class FakeRouter:
    def __init__(self):
        self.handled = []

    async def handle(self, parsed_msg):
        self.handled.append(parsed_msg)


def make_settings(**overrides) -> Settings:
    values = {"EVOLUTION_WEBHOOK_SECRET": SECRET, "EVOLUTION_INSTANCE": "test-instance"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # never read the developer's real .env in tests


@pytest.fixture
def api():
    fake = FakeRouter()
    # Zero-argument callables: FastAPI would treat a `**kwargs` signature as required query parameters
    app.dependency_overrides[get_settings] = lambda: make_settings()
    app.dependency_overrides[get_message_router] = lambda: fake
    yield TestClient(app), fake
    app.dependency_overrides.clear()


def test_messages_upsert_is_dispatched_to_the_router_in_the_background(api, sample_webhook_text_payload):
    client, router = api

    response = client.post(URL, json=sample_webhook_text_payload, headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"status": "EVENT_RECEIVED"}
    assert len(router.handled) == 1
    assert router.handled[0].sender_phone == "5491122334455"
    assert router.handled[0].text_content == "Gasté 500 en Uber con crédito"


def test_the_event_name_in_the_config_spelling_is_accepted_too(api, make_evolution_payload):
    client, router = api

    client.post(URL, json=make_evolution_payload({"conversation": "hola"}, event="MESSAGES_UPSERT"), headers=HEADERS)

    assert len(router.handled) == 1


def test_by_events_url_suffix_is_supported(api, sample_webhook_text_payload):
    """Evolution appends /messages-upsert to the URL when "webhook by events" is on."""
    client, router = api

    response = client.post(f"{URL}/messages-upsert", json=sample_webhook_text_payload, headers=HEADERS)

    assert response.status_code == 200
    assert len(router.handled) == 1


@pytest.mark.parametrize("headers", [{}, {"x-webhook-secret": "wrong"}, {"x-webhook-secret": ""}, {"x-webhook-secret": SECRET + "x"}])
def test_calls_without_the_right_secret_are_rejected_and_nothing_is_processed(api, sample_webhook_text_payload, headers):
    client, router = api

    response = client.post(URL, json=sample_webhook_text_payload, headers=headers)

    assert response.status_code == 401
    assert router.handled == []


def test_fails_closed_when_no_secret_is_configured_even_if_the_caller_sends_one(sample_webhook_text_payload):
    fake = FakeRouter()
    app.dependency_overrides[get_settings] = lambda: make_settings(EVOLUTION_WEBHOOK_SECRET="")
    app.dependency_overrides[get_message_router] = lambda: fake
    try:
        client = TestClient(app)
        assert client.post(URL, json=sample_webhook_text_payload, headers=HEADERS).status_code == 503
        assert client.post(URL, json=sample_webhook_text_payload, headers={"x-webhook-secret": ""}).status_code == 503
        assert client.post(URL, json=sample_webhook_text_payload).status_code == 503
        assert fake.handled == []
    finally:
        app.dependency_overrides.clear()


def test_non_message_events_are_acknowledged_and_ignored(api, make_evolution_payload):
    client, router = api

    for event in ("connection.update", "messages.update", "send.message", "qrcode.updated"):
        response = client.post(URL, json=make_evolution_payload({"conversation": "hola"}, event=event), headers=HEADERS)
        assert response.json() == {"status": "IGNORED"}

    assert router.handled == []


def test_events_from_another_instance_are_ignored(api, make_evolution_payload):
    client, router = api

    response = client.post(URL, json=make_evolution_payload({"conversation": "hola"}, instance="other"), headers=HEADERS)

    assert response.json() == {"status": "IGNORED"}
    assert router.handled == []


def test_the_bots_own_outgoing_messages_are_acknowledged_but_never_processed(api, make_evolution_payload):
    client, router = api

    response = client.post(URL, json=make_evolution_payload({"conversation": "✅ listo"}, from_me=True), headers=HEADERS)

    assert response.status_code == 200
    assert router.handled == []


def test_invalid_json_is_a_400(api):
    client, router = api

    response = client.post(URL, content=b"{not json", headers={**HEADERS, "content-type": "application/json"})

    assert response.status_code == 400
    assert router.handled == []


def test_the_instance_api_key_in_the_payload_is_never_logged(api, sample_webhook_text_payload, caplog):
    client, _ = api

    with caplog.at_level(logging.DEBUG, logger="bot_wpp"):
        client.post(URL, json=sample_webhook_text_payload, headers=HEADERS)
        client.post(URL, json=sample_webhook_text_payload, headers={"x-webhook-secret": "wrong"})

    assert "FAKE-INSTANCE-API-KEY" not in caplog.text
    assert SECRET not in caplog.text
    assert "5491122334455" not in caplog.text  # phones are masked


def test_meta_graph_api_endpoints_are_gone(api, sample_webhook_text_payload):
    client, _ = api

    handshake = client.get(
        "/api/v1/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1234"},
    )
    assert handshake.status_code == 404
    assert client.post("/api/v1/webhooks/whatsapp", json={"entry": []}).status_code == 404
    assert client.get(URL, params={"hub.challenge": "1234"}).status_code == 405
