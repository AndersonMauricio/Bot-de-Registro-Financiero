"""Unit tests for the Evolution API client: HTTP contracts and the typing presence."""

import asyncio
import base64
import json
import time
import httpx
import pytest
from src.core.exceptions import WhatsAppClientError
from src.infrastructure.whatsapp.client import EvolutionAPIClient, decode_base64_media

pytestmark = pytest.mark.asyncio

PHONE = "5491100000001"


class Gateway:
    """Fake Evolution API behind httpx.MockTransport."""

    def __init__(self, status: int = 200, body: dict = None, hold: float = 0.0):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = {"ok": True} if body is None else body
        self.hold = hold  # like Evolution's sendPresence, keep the request open for a while

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.hold:
            await asyncio.sleep(self.hold)
        return httpx.Response(self.status, json=self.body)

    def json(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)

    def calls_to(self, suffix: str) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith(suffix))


def make_client(gateway: Gateway, **kwargs) -> EvolutionAPIClient:
    defaults = dict(
        base_url="http://evolution.test/",
        api_key="test-api-key",
        instance="my-instance",
        transport=httpx.MockTransport(gateway),
    )
    return EvolutionAPIClient(**{**defaults, **kwargs})


async def test_send_text_message_contract():
    gateway = Gateway()

    assert await make_client(gateway).send_text_message(PHONE, "hola ✅") is True

    request = gateway.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://evolution.test/message/sendText/my-instance"
    assert request.headers["apikey"] == "test-api-key"
    assert gateway.json() == {"number": PHONE, "text": "hola ✅"}


async def test_send_text_message_returns_false_instead_of_raising_on_gateway_errors(caplog):
    gateway = Gateway(status=500)

    with caplog.at_level("ERROR", logger="bot_wpp"):
        assert await make_client(gateway).send_text_message(PHONE, "hola") is False

    assert PHONE not in caplog.text  # phones are masked in logs


async def test_nothing_is_sent_when_the_client_is_not_configured():
    gateway = Gateway()
    client = make_client(gateway, api_key="")

    assert await client.send_text_message(PHONE, "hola") is False
    assert await client.send_presence(PHONE) is False
    assert gateway.requests == []


async def test_instance_name_is_url_encoded():
    gateway = Gateway()

    await make_client(gateway, instance="mi instancia/1").send_text_message(PHONE, "hola")

    assert gateway.requests[0].url.raw_path.decode().endswith("/message/sendText/mi%20instancia%2F1")


async def test_send_presence_contract():
    gateway = Gateway()

    assert await make_client(gateway).send_presence(PHONE, "composing", 3000) is True

    request = gateway.requests[0]
    assert str(request.url) == "http://evolution.test/chat/sendPresence/my-instance"
    # v2 body: flat, and `delay` is mandatory
    assert gateway.json() == {"number": PHONE, "presence": "composing", "delay": 3000}


async def test_typing_keeps_the_presence_alive_while_the_block_runs_and_stops_after():
    gateway = Gateway()
    client = make_client(gateway, presence_chunk_ms=40)

    async with client.typing(PHONE):
        await asyncio.sleep(0.25)
        during = gateway.calls_to("/sendPresence/my-instance")
    await asyncio.sleep(0.1)
    after = gateway.calls_to("/sendPresence/my-instance")

    assert during >= 3
    assert after == during or after == during + 1  # at most one call that was already in flight
    await asyncio.sleep(0.15)
    assert gateway.calls_to("/sendPresence/my-instance") == after
    assert all(gateway.json(i)["presence"] == "composing" for i in range(len(gateway.requests)))


async def test_typing_does_not_wait_for_a_presence_request_the_gateway_is_still_holding():
    """Evolution's sendPresence blocks for `delay` ms: leaving the block must cancel it, not wait for it."""
    gateway = Gateway(hold=5.0)
    client = make_client(gateway, presence_chunk_ms=20_000)

    started = time.perf_counter()
    async with client.typing(PHONE):
        await asyncio.sleep(0.1)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0
    assert gateway.calls_to("/sendPresence/my-instance") == 1


async def test_typing_failures_are_swallowed_and_stop_the_presence_loop():
    gateway = Gateway(status=500)
    client = make_client(gateway, presence_chunk_ms=20)

    async with client.typing(PHONE):  # must not raise
        await asyncio.sleep(0.15)

    assert gateway.calls_to("/sendPresence/my-instance") == 1  # no retry storm for a cosmetic feature


async def test_typing_does_not_hide_errors_from_the_wrapped_block():
    client = make_client(Gateway(), presence_chunk_ms=20)

    with pytest.raises(ValueError, match="boom"):
        async with client.typing(PHONE):
            raise ValueError("boom")


async def test_download_media_returns_the_decoded_bytes():
    audio = b"OggS-fake-audio"
    gateway = Gateway(body={"mediaType": "audioMessage", "mimetype": "audio/ogg", "base64": base64.b64encode(audio).decode()})

    assert await make_client(gateway).download_media("3EB0A1B2C3D4E5F60002") == audio

    assert str(gateway.requests[0].url) == "http://evolution.test/chat/getBase64FromMediaMessage/my-instance"
    assert gateway.json() == {"message": {"key": {"id": "3EB0A1B2C3D4E5F60002"}}, "convertToMp4": False}


@pytest.mark.parametrize(
    "gateway",
    [
        Gateway(status=400, body={"message": "Message not found"}),
        Gateway(body={"mediaType": "audioMessage"}),  # no base64
        Gateway(body={"base64": ""}),  # empty (seen when media storage is not enabled)
        Gateway(body={"base64": "@@@"}),
    ],
)
async def test_download_media_failures_raise_a_domain_error(gateway):
    with pytest.raises(WhatsAppClientError):
        await make_client(gateway).download_media("msg")


async def test_set_webhook_sends_the_v2_body_with_the_secret_header_and_only_messages_upsert():
    gateway = Gateway(body={"webhook": {"enabled": True}})

    result = await make_client(gateway).set_webhook("https://bot.example.com/api/v1/webhooks/evolution", "s3cret")

    assert str(gateway.requests[0].url) == "http://evolution.test/webhook/set/my-instance"
    assert gateway.json() == {
        "webhook": {
            "enabled": True,
            "url": "https://bot.example.com/api/v1/webhooks/evolution",
            "headers": {"x-webhook-secret": "s3cret"},
            "byEvents": False,
            "base64": True,
            "events": ["MESSAGES_UPSERT"],
        }
    }
    assert result == {"webhook": {"enabled": True}}


async def test_set_webhook_error_is_a_domain_error_and_never_leaks_the_secret():
    with pytest.raises(WhatsAppClientError) as exc:
        await make_client(Gateway(status=401)).set_webhook("https://bot.example.com/hook", "s3cret-value")
    assert "s3cret-value" not in str(exc.value)


async def test_decode_base64_media_accepts_data_uris_and_rejects_empty_content():
    assert decode_base64_media("data:audio/ogg;base64," + base64.b64encode(b"abc").decode()) == b"abc"
    with pytest.raises(WhatsAppClientError):
        decode_base64_media("")
