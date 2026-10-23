"""Evolution API client (self-hosted WhatsApp gateway): messages, typing presence, media and webhook setup."""

import asyncio
import base64
import binascii
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, Sequence
from urllib.parse import quote
import httpx
from src.core.config import settings
from src.core.exceptions import WhatsAppClientError
from src.core.logging import logger
from src.core.phone import mask_phone

# Evolution's sendPresence holds the request for `delay` ms and then sends "paused"; WhatsApp itself
# drops the "typing..." indicator after ~25 s. One request per chunk keeps it alive without gaps.
PRESENCE_CHUNK_MS = 20_000


def decode_base64_media(data: str) -> bytes:
    """Decode media delivered by Evolution as base64 (optionally as a data URI)."""
    if data.startswith("data:") and "," in data:
        data = data.split(",", 1)[1]
    try:
        content = base64.b64decode(data)
    except (binascii.Error, ValueError) as e:
        raise WhatsAppClientError(f"Invalid base64 media: {e}") from e
    if not content:
        raise WhatsAppClientError("Media content is empty.")
    return content


class EvolutionAPIClient:
    """Client for Evolution API v2."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        instance: Optional[str] = None,
        presence_chunk_ms: int = PRESENCE_CHUNK_MS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.base_url = (base_url or settings.EVOLUTION_API_URL).rstrip("/")
        self.api_key = settings.EVOLUTION_API_KEY.get_secret_value() if api_key is None else api_key
        self.instance = settings.EVOLUTION_INSTANCE if instance is None else instance
        self.presence_chunk_ms = presence_chunk_ms
        self._transport = transport

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.instance)

    @property
    def headers(self) -> dict:
        return {"apikey": self.api_key, "Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path}/{quote(self.instance, safe='')}"

    def _http(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    async def send_text_message(self, recipient_phone: str, text: str) -> bool:
        """Send a plain text message to a WhatsApp user."""
        if not self.is_configured:
            logger.warning("Evolution API credentials missing; skipping sending text message.")
            return False

        payload = {"number": recipient_phone, "text": text}

        async with self._http(timeout=15.0) as client:
            try:
                response = await client.post(self._url("message/sendText"), headers=self.headers, json=payload)
                response.raise_for_status()
                logger.info(f"WhatsApp reply sent successfully to {mask_phone(recipient_phone)}.")
                return True
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Failed to send WhatsApp message to {mask_phone(recipient_phone)}: HTTP {e.response.status_code}"
                )
                return False
            except httpx.HTTPError as e:
                logger.error(f"Failed to send WhatsApp message to {mask_phone(recipient_phone)}: {e!r}")
                return False

    async def send_presence(
        self,
        recipient_phone: str,
        presence: str = "composing",
        duration_ms: Optional[int] = None,
    ) -> bool:
        """Send a chat presence ("composing", "recording", "paused"). The call returns after `duration_ms`."""
        if not self.is_configured:
            return False

        duration = self.presence_chunk_ms if duration_ms is None else duration_ms
        payload = {"number": recipient_phone, "presence": presence, "delay": duration}

        async with self._http(timeout=duration / 1000 + 10.0) as client:
            try:
                response = await client.post(self._url("chat/sendPresence"), headers=self.headers, json=payload)
                response.raise_for_status()
                return True
            except httpx.HTTPStatusError as e:
                logger.warning(f"Presence update failed for {mask_phone(recipient_phone)}: HTTP {e.response.status_code}")
                return False
            except httpx.HTTPError as e:
                logger.warning(f"Presence update failed for {mask_phone(recipient_phone)}: {e!r}")
                return False

    @asynccontextmanager
    async def typing(self, recipient_phone: str) -> AsyncIterator[None]:
        """Show "typing..." to the recipient while the wrapped block runs.

        Purely cosmetic: failures are logged and never propagate. The wrapped block must not block the event
        loop (run synchronous SDK calls in a thread), otherwise the presence requests cannot be sent meanwhile.
        """
        task = asyncio.create_task(self._keep_composing(recipient_phone))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _keep_composing(self, recipient_phone: str) -> None:
        loop = asyncio.get_running_loop()
        chunk_seconds = self.presence_chunk_ms / 1000
        try:
            while True:
                started = loop.time()
                if not await self.send_presence(recipient_phone, "composing", self.presence_chunk_ms):
                    return  # stop on the first failure instead of retrying a cosmetic feature
                # A gateway that answers immediately must not turn this into a hot loop
                await asyncio.sleep(max(0.0, chunk_seconds - (loop.time() - started)))
        except Exception as e:
            logger.warning(f"Typing presence stopped unexpectedly: {e!r}")

    async def download_media(self, message_id: str) -> bytes:
        """Download a received media message (already decrypted) through Evolution's getBase64FromMediaMessage."""
        if not self.is_configured:
            raise WhatsAppClientError("Evolution API credentials missing for media download.")

        payload = {"message": {"key": {"id": message_id}}, "convertToMp4": False}

        async with self._http(timeout=30.0) as client:
            try:
                response = await client.post(
                    self._url("chat/getBase64FromMediaMessage"), headers=self.headers, json=payload
                )
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as e:
                logger.error(f"Error downloading WhatsApp media for message {message_id}: {e!r}")
                raise WhatsAppClientError(f"Evolution media download failed: {e!r}") from e

        encoded = body.get("base64") if isinstance(body, dict) else None
        if not encoded:
            raise WhatsAppClientError("Evolution API response did not include base64 media.")
        return decode_base64_media(encoded)

    async def set_webhook(
        self,
        url: str,
        secret: str,
        events: Sequence[str] = ("MESSAGES_UPSERT",),
        include_base64: bool = True,
    ) -> dict:
        """Point the instance webhook at `url` (Evolution API v2 body); `secret` comes back as `x-webhook-secret`.

        `include_base64` makes Evolution attach media (voice notes) inline in the payload.
        """
        if not self.is_configured:
            raise WhatsAppClientError("Evolution API credentials missing; cannot configure the webhook.")

        payload = {
            "webhook": {
                "enabled": True,
                "url": url,
                "headers": {"x-webhook-secret": secret},
                "byEvents": False,
                "base64": include_base64,
                "events": list(events),
            }
        }

        async with self._http(timeout=15.0) as client:
            try:
                response = await client.post(self._url("webhook/set"), headers=self.headers, json=payload)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as e:
                raise WhatsAppClientError(f"Evolution webhook configuration failed: {e!r}") from e
