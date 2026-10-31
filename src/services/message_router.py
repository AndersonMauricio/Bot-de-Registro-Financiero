"""Message router: gates every incoming message by the sender's access state before any LLM work."""

import asyncio
import weakref
from typing import Optional
from src.core.config import settings
from src.core.exceptions import StorageError
from src.core.logging import logger
from src.core.phone import mask_phone, phone_key
from src.domain.enums import UserStatusEnum
from src.infrastructure.cache.ttl_cache import TTLCache
from src.infrastructure.storage.base import UserRepositoryInterface
from src.infrastructure.whatsapp.client import EvolutionAPIClient
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services.expense_service import ExpenseProcessingService
from src.services.onboarding_service import OnboardingService

ACCESS_DENIED_MESSAGE = (
    "⛔ Acceso denegado. Tu número no está autorizado para usar este servicio. "
    "Si creés que se trata de un error, contactá al administrador."
)
ACCESS_CHECK_UNAVAILABLE_MESSAGE = (
    "⚠️ No pudimos verificar tu acceso en este momento. Por favor intentá de nuevo en unos minutos."
)
UNEXPECTED_ERROR_MESSAGE = "⚠️ Ocurrió un error inesperado procesando tu mensaje. Por favor intentá de nuevo más tarde."

# How long processed message ids are remembered to discard duplicate webhook deliveries
SEEN_MESSAGES_TTL_SECONDS = 600


class MessageRouter:
    """Routes each incoming message by the sender's access state (Google Sheets):

    - approved (1)    -> expense registration flow
    - rejected (2)    -> standard denial, the flow stops
    - unknown (null)  -> onboarding flow (data collection, then manual approval by an administrator)

    Any failure to determine the state denies access: nothing is registered without a verified approval.
    """

    def __init__(
        self,
        user_repository: UserRepositoryInterface,
        expense_service: ExpenseProcessingService,
        onboarding_service: OnboardingService,
        whatsapp_client: EvolutionAPIClient,
        seen_messages: Optional[TTLCache] = None,
        max_concurrent: Optional[int] = None,
    ):
        self.user_repository = user_repository
        self.expense_service = expense_service
        self.onboarding_service = onboarding_service
        self.whatsapp_client = whatsapp_client
        self._seen: TTLCache = seen_messages or TTLCache(SEEN_MESSAGES_TTL_SECONDS)
        # Locks disappear on their own once no message from that sender is being processed
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        # A burst is queued instead of firing one LLM/Sheets call per message at once (Groq answers 429)
        self._slots = asyncio.Semaphore(max_concurrent or settings.MAX_CONCURRENT_MESSAGES)
        # Messages being processed or waiting for a slot, so a shutdown can wait for them
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def _begin(self) -> None:
        self._active += 1
        self._idle.clear()

    def _end(self) -> None:
        self._active -= 1
        if self._active <= 0:
            self._idle.set()

    async def wait_until_idle(self, timeout: float) -> int:
        """Wait up to `timeout` seconds for the messages in flight; return how many are still pending."""
        if self._active <= 0 or timeout <= 0:
            return max(self._active, 0)
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Still {self._active} message(s) in flight after waiting {timeout:.0f}s.")
        return max(self._active, 0)

    def _lock_for(self, phone: str) -> asyncio.Lock:
        key = phone_key(phone)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def handle(self, parsed_msg: ParsedWhatsAppMessage) -> None:
        """Process one incoming message. Never raises: it runs in the background after the webhook answered."""
        phone = parsed_msg.sender_phone

        # Evolution retries deliveries; a message must never be processed (or answered) twice
        if not self._seen.set_if_absent(parsed_msg.message_id, True):
            logger.info(f"Duplicate delivery of message {parsed_msg.message_id} ignored.")
            return

        self._begin()
        try:
            # One message at a time per sender keeps onboarding state consistent (no duplicated rows or
            # replies); the global slot bounds how many messages reach Groq and Sheets at once
            async with self._lock_for(phone):
                async with self._slots:
                    try:
                        await self._dispatch(parsed_msg)
                    except Exception:
                        logger.exception(
                            f"Unhandled error processing message {parsed_msg.message_id} from {mask_phone(phone)}"
                        )
                        await self.whatsapp_client.send_text_message(phone, UNEXPECTED_ERROR_MESSAGE)
        finally:
            self._end()

    async def _dispatch(self, parsed_msg: ParsedWhatsAppMessage) -> None:
        phone = parsed_msg.sender_phone

        try:
            user = await asyncio.to_thread(self.user_repository.get_user, phone)
        except StorageError as e:
            logger.error(f"Could not verify access for {mask_phone(phone)}: {e}")
            await self.whatsapp_client.send_text_message(phone, ACCESS_CHECK_UNAVAILABLE_MESSAGE)
            return

        status = user.status if user else None

        if status == UserStatusEnum.APPROVED:
            await self.expense_service.process_incoming_message(parsed_msg)
        elif status == UserStatusEnum.REJECTED:
            logger.info(f"Access denied for {mask_phone(phone)} (state 2).")
            await self.whatsapp_client.send_text_message(phone, ACCESS_DENIED_MESSAGE)
        else:
            logger.info(f"Unknown access state for {mask_phone(phone)}: starting/continuing onboarding.")
            await self.onboarding_service.handle(parsed_msg, user)
