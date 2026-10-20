"""WhatsApp webhook endpoints for verification and message ingestion."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from src.api.v1.dependencies import get_expense_service, get_settings
from src.core.config import Settings
from src.core.logging import logger
from src.infrastructure.whatsapp.parser import WhatsAppPayloadParser
from src.services.expense_service import ExpenseProcessingService

router = APIRouter(prefix="/webhooks/whatsapp", tags=["Webhooks"])


@router.get("")
async def verify_webhook(
    mode: str = Query(..., alias="hub.mode"),
    token: str = Query(..., alias="hub.verify_token"),
    challenge: str = Query(..., alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
):
    """Handle Meta WhatsApp Webhook verification handshake."""
    logger.info("Received WhatsApp webhook verification request.")
    if mode == "subscribe" and token == settings.WHATSAPP_VERIFY_TOKEN:
        logger.info("Webhook verification token accepted.")
        return Response(content=challenge, media_type="text/plain")

    logger.warning("Webhook verification failed: token mismatch.")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Verification token mismatch",
    )


@router.post("")
async def receive_webhook_event(
    request: Request,
    background_tasks: BackgroundTasks,
    expense_service: ExpenseProcessingService = Depends(get_expense_service),
):
    """Receive incoming WhatsApp messages and dispatch asynchronous processing."""
    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Invalid JSON payload received in webhook: {e}")
        return Response(content="INVALID_JSON", status_code=status.HTTP_400_BAD_REQUEST)

    parsed_message = WhatsAppPayloadParser.parse_webhook(payload)

    if parsed_message:
        logger.info(
            f"Dispatched background task for message {parsed_message.message_id} from {parsed_message.sender_phone}"
        )
        # Asynchronously execute processing to satisfy WhatsApp's 3-second response window
        background_tasks.add_task(
            expense_service.process_incoming_message, parsed_message
        )

    # Always return HTTP 200 OK to acknowledge receipt to WhatsApp API
    return {"status": "EVENT_RECEIVED"}
