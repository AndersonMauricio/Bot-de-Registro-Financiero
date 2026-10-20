"""WhatsApp Cloud API client for sending messages and downloading media."""

import httpx
from src.core.config import settings
from src.core.exceptions import WhatsAppClientError
from src.core.logging import logger


class WhatsAppClient:
    """Client for Meta's WhatsApp Cloud API."""

    def __init__(self):
        self.api_version = settings.WHATSAPP_API_VERSION
        self.phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
        self.token = settings.WHATSAPP_TOKEN.get_secret_value()
        self.base_url = f"https://graph.facebook.com/{self.api_version}"

    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def send_text_message(self, recipient_phone: str, text: str) -> bool:
        """Send a plain text message to a WhatsApp user."""
        if not (self.token and self.phone_number_id):
            logger.warning("WhatsApp credentials missing; skipping sending text message.")
            return False

        url = f"{self.base_url}/{self.phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_phone,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(url, headers=self.headers, json=payload)
                response.raise_for_status()
                logger.info(f"WhatsApp reply sent successfully to {recipient_phone}.")
                return True
            except httpx.HTTPError as e:
                logger.error(f"Failed to send WhatsApp message to {recipient_phone}: {e}")
                return False

    async def download_media(self, media_id: str) -> bytes:
        """Fetch media URL and download binary content using media_id."""
        if not self.token:
            raise WhatsAppClientError("Missing WHATSAPP_TOKEN for media download.")

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                # 1. Query media metadata endpoint to get download URL
                meta_url = f"{self.base_url}/{media_id}"
                meta_res = await client.get(
                    meta_url, headers={"Authorization": f"Bearer {self.token}"}
                )
                meta_res.raise_for_status()
                media_download_url = meta_res.json().get("url")

                if not media_download_url:
                    raise WhatsAppClientError("Media metadata did not contain a valid download URL.")

                # 2. Download binary bytes
                media_res = await client.get(
                    media_download_url,
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                media_res.raise_for_status()
                return media_res.content

            except httpx.HTTPError as e:
                logger.error(f"Error downloading WhatsApp media {media_id}: {e}")
                raise WhatsAppClientError(f"WhatsApp media download failed: {e}") from e
