"""Access notifier: tells a user over WhatsApp when an administrator approves or rejects them.

The Apps Script trigger only reports *that* the `estado` column was edited. The notice is decided by
re-reading the users sheet (never by the payload), so a leaked webhook secret cannot be used to message
an arbitrary number.
"""

import asyncio
from dataclasses import dataclass
from typing import Optional
from src.core.exceptions import StorageError
from src.core.logging import logger
from src.core.phone import mask_phone, phone_key
from src.domain.enums import UserStatusEnum
from src.domain.models import UserRecord
from src.infrastructure.cache.ttl_cache import TTLCache
from src.infrastructure.storage.base import UserRepositoryInterface
from src.infrastructure.storage.sheets_users_repo import parse_status
from src.infrastructure.whatsapp.client import EvolutionAPIClient

APPROVED_MESSAGE_HEADER = "✅ ¡Hola{name}! Tu cuenta ya fue aprobada."
APPROVED_MESSAGE_BODY = (
    "Ya podés registrarme tus gastos por acá: escribime, mandame un audio o una foto del ticket.\n\n"
    "Por ejemplo: «Gasté 5000 en el super con débito»."
)
REJECTED_MESSAGE = (
    "⛔ Tu solicitud de acceso no fue aprobada. "
    "Si creés que se trata de un error, contactá al administrador."
)
# A phone is not told about the same state twice within this window (re-saved cells, retried webhooks)
NOTICE_TTL_SECONDS = 3600


@dataclass(frozen=True)
class AccessChange:
    """An edit reported by the Apps Script trigger, with the sheet values already parsed."""

    phone: str
    estado: Optional[UserStatusEnum] = None            # new cell value, as read by the script
    estado_anterior: Optional[UserStatusEnum] = None   # previous value (single-cell edits only)

    @classmethod
    def build(cls, phone: str, estado=None, estado_anterior=None) -> "AccessChange":
        """Parse the raw cell values sent by the script (numbers, strings or empty cells)."""
        return cls(
            phone=phone,
            estado=parse_status(estado),
            estado_anterior=parse_status(estado_anterior),
        )


class AccessNotifier:
    """Sends the approval / rejection notice for phones whose `estado` was just edited.

    Runs after the webhook answered (background task), one phone at a time: the first sheet read of a
    batch refreshes the whole table, so a paste of many rows still costs a single Sheets request.
    """

    def __init__(
        self,
        user_repository: UserRepositoryInterface,
        whatsapp_client: EvolutionAPIClient,
        notified: Optional[TTLCache] = None,
    ):
        self.user_repository = user_repository
        self.whatsapp_client = whatsapp_client
        self._notified: TTLCache = notified or TTLCache(NOTICE_TTL_SECONDS)

    async def notify(self, changes: list[AccessChange]) -> None:
        """Announce every edited phone. Never raises: it runs in the background after the webhook response."""
        for change in self._unique_by_phone(changes):
            try:
                await self._notify_one(change)
            except Exception as e:
                logger.error(f"Could not notify {mask_phone(change.phone)} of an access change: {e}")

    async def _notify_one(self, change: AccessChange) -> None:
        if change.estado is not None and change.estado == change.estado_anterior:
            return  # the administrator re-entered the same value: nothing changed

        try:
            # The endpoint invalidated this phone, so this is a forced, fail-closed read of the sheet
            user = await asyncio.to_thread(self.user_repository.get_user, change.phone)
        except StorageError as e:
            logger.warning(f"Users sheet unavailable; no access notice sent to {mask_phone(change.phone)}: {e}")
            return

        status = user.status if user else None
        if status is None:
            return  # pending (or the row is gone): nothing to announce

        if change.estado is not None and change.estado != status:
            logger.warning(
                f"Users sheet changed again for {mask_phone(change.phone)}; "
                f"announcing the state read from the sheet ({status.value})."
            )
        if status == change.estado_anterior:
            return  # the sheet says what it already said before the edit

        # Atomic reservation: two concurrent edits (or a retried webhook) must not message the user twice
        key = f"{phone_key(change.phone)}:{status.value}"
        if not self._notified.set_if_absent(key, True):
            logger.info(f"Access notice already sent to {mask_phone(change.phone)} recently; skipping.")
            return

        if not await self.whatsapp_client.send_text_message(change.phone, self._message_for(status, user)):
            self._notified.delete(key)
            logger.warning(f"Access notice for {mask_phone(change.phone)} could not be delivered.")
            return

        logger.info(f"Access notice ({status.name.lower()}) sent to {mask_phone(change.phone)}.")

    @staticmethod
    def _message_for(status: UserStatusEnum, user: Optional[UserRecord]) -> str:
        if status != UserStatusEnum.APPROVED:
            return REJECTED_MESSAGE
        first_name = (user.full_name or "").split()[0] if user and user.full_name else ""
        header = APPROVED_MESSAGE_HEADER.format(name=f", {first_name}" if first_name else "")
        return f"{header}\n\n{APPROVED_MESSAGE_BODY}"

    @staticmethod
    def _unique_by_phone(changes: list[AccessChange]) -> list[AccessChange]:
        """One notice per phone (a pasted range may repeat a number); the last edit reported wins."""
        unique: dict[str, AccessChange] = {}
        for change in changes:
            key = phone_key(change.phone)
            if key:
                unique[key] = change
        return list(unique.values())
