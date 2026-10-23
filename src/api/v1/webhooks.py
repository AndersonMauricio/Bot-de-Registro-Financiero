"""Evolution API webhook endpoint: ingestion of WhatsApp messages (messages.upsert)."""

import hmac
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator
from src.api.v1.dependencies import get_message_router, get_settings, get_user_repository
from src.core.config import Settings
from src.core.logging import logger
from src.core.phone import mask_phone, normalize_phone
from src.infrastructure.storage.base import UserRepositoryInterface
from src.infrastructure.whatsapp.parser import WhatsAppPayloadParser
from src.services.message_router import MessageRouter

router = APIRouter(prefix="/webhooks/evolution", tags=["Webhooks"])
sheets_router = APIRouter(prefix="/webhooks/sheets", tags=["Webhooks"])


def _check_secret(provided: Optional[str], expected: str, variable: str) -> None:
    """Constant-time comparison of a shared secret. Fails closed when the secret is not configured."""
    if not expected:
        logger.error(f"{variable} is not configured; rejecting webhook call.")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Webhook secret not configured")

    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        logger.warning("Webhook call rejected: invalid secret.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret")


def verify_webhook_secret(
    x_webhook_secret: Optional[str] = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate Evolution API calls with the shared secret set in the instance webhook headers.

    Without it anyone who finds the URL could forge messages from an approved number.
    Fails closed: if the secret is not configured, every call is rejected.
    """
    _check_secret(x_webhook_secret, settings.EVOLUTION_WEBHOOK_SECRET.get_secret_value(), "EVOLUTION_WEBHOOK_SECRET")


def verify_sheets_secret(
    x_webhook_secret: Optional[str] = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate the Google Apps Script trigger (its own secret, separate from Evolution's)."""
    _check_secret(x_webhook_secret, settings.SHEETS_WEBHOOK_SECRET.get_secret_value(), "SHEETS_WEBHOOK_SECRET")


@router.post("", dependencies=[Depends(verify_webhook_secret)])
# Evolution appends the event name to the URL when "webhook by events" is enabled
@router.post("/messages-upsert", dependencies=[Depends(verify_webhook_secret)], include_in_schema=False)
async def receive_evolution_event(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    message_router: MessageRouter = Depends(get_message_router),
):
    """Receive Evolution API `messages.upsert` events and dispatch asynchronous processing."""
    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Invalid JSON payload received in webhook: {e}")
        return Response(content="INVALID_JSON", status_code=status.HTTP_400_BAD_REQUEST)

    # The payload carries the instance API key: never log it whole
    if not isinstance(payload, dict) or not WhatsAppPayloadParser.is_messages_upsert(payload):
        return {"status": "IGNORED"}

    if settings.EVOLUTION_INSTANCE and payload.get("instance") != settings.EVOLUTION_INSTANCE:
        logger.warning("Ignored webhook event from an unexpected Evolution instance.")
        return {"status": "IGNORED"}

    parsed_message = WhatsAppPayloadParser.parse_webhook(payload)

    if parsed_message:
        logger.info(
            f"Dispatched background task for message {parsed_message.message_id} from {mask_phone(parsed_message.sender_phone)}"
        )
        # Process asynchronously: Evolution times out slow webhooks and then retries them
        background_tasks.add_task(message_router.handle, parsed_message)

    # Always return HTTP 200 OK so Evolution does not retry the delivery
    return {"status": "EVENT_RECEIVED"}


class SheetsEvent(BaseModel):
    """Payload sent by the Apps Script trigger of the users tab."""
    event: str = "estado_changed"
    sheet: Optional[str] = None
    phone: Optional[str] = None

    @field_validator("phone")
    @classmethod
    def phone_must_have_digits(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        digits = normalize_phone(v)
        if not (6 <= len(digits) <= 20):
            raise ValueError("phone must contain between 6 and 20 digits")
        return digits


@sheets_router.post("", dependencies=[Depends(verify_sheets_secret)])
async def receive_sheets_event(
    payload: SheetsEvent,
    settings: Settings = Depends(get_settings),
    user_repository: UserRepositoryInterface = Depends(get_user_repository),
):
    """An administrator edited the users tab: forget what is cached for that phone (or for everyone)."""
    if payload.event == "ping":
        return {"status": "PONG"}

    if payload.sheet and payload.sheet != settings.USERS_WORKSHEET_NAME:
        return {"status": "IGNORED"}

    if payload.phone:
        user_repository.invalidate(payload.phone)
        logger.info(f"Users cache invalidated for {mask_phone(payload.phone)}.")
        return {"status": "INVALIDATED"}

    user_repository.invalidate_all()
    logger.info("Users cache invalidated for every user.")
    return {"status": "INVALIDATED_ALL"}
