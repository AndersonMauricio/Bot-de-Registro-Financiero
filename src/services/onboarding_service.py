"""Onboarding service: collects registration data from unknown users while their account awaits manual approval."""

import asyncio
from typing import Optional
from src.core.logging import logger
from src.core.phone import mask_phone
from src.domain.enums import InputTypeEnum
from src.domain.models import OnboardingData, UserRecord
from src.infrastructure.llm.groq_client import GroqLLMClient
from src.infrastructure.storage.base import UserRepositoryInterface
from src.infrastructure.whatsapp.client import EvolutionAPIClient
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services.data_quality import DataQualityGate

# Fixed copy: the LLM drafts questions, but it never gets to announce approval status
PENDING_APPROVAL_MESSAGE = (
    "✅ ¡Gracias! Recibimos tus datos.\n\n"
    "Tu cuenta requiere aprobación manual del administrador antes de poder registrar gastos. "
    "Una vez aprobada, vas a poder enviarme tus gastos por acá."
)
PENDING_REMINDER_MESSAGE = (
    "⏳ Tu cuenta todavía está pendiente de aprobación por parte del administrador. "
    "Hasta que sea aprobada no puedo registrar tus gastos."
)
ONBOARDING_ERROR_MESSAGE = "⚠️ Tuvimos un problema procesando tu mensaje. Por favor intentá de nuevo en unos minutos."
FIELD_LABELS = {"full_name": "tu nombre y apellido", "email": "tu email"}


class OnboardingService:
    """Conversation flow for users whose access state is unknown (not approved, not rejected)."""

    def __init__(
        self,
        groq_client: GroqLLMClient,
        user_repository: UserRepositoryInterface,
        whatsapp_client: EvolutionAPIClient,
    ):
        self.groq_client = groq_client
        self.user_repository = user_repository
        self.whatsapp_client = whatsapp_client

    async def handle(self, parsed_msg: ParsedWhatsAppMessage, user: Optional[UserRecord]) -> None:
        """Advance the onboarding of an unknown user. Never registers expenses."""
        phone = parsed_msg.sender_phone

        # First contact: register the number right away, so the administrator sees who wrote (and when),
        # even if the person never sends their data
        if user is None:
            try:
                user = await asyncio.to_thread(self.user_repository.save_onboarding, phone, OnboardingData())
            except Exception as e:
                logger.error(f"Could not register new user {mask_phone(phone)}: {e}")
                await self.whatsapp_client.send_text_message(phone, ONBOARDING_ERROR_MESSAGE)
                return

        current = user.onboarding_data()

        # Everything was already collected: just wait for the administrator (no LLM call, no cost)
        if current.is_complete:
            await self.whatsapp_client.send_text_message(phone, PENDING_REMINDER_MESSAGE)
            return

        # Onboarding is text-only: no transcription or OCR is spent on users who are not approved
        if parsed_msg.input_type != InputTypeEnum.TEXT:
            label = FIELD_LABELS[current.missing_fields()[0]]
            await self.whatsapp_client.send_text_message(
                phone, f"Para completar tu alta necesito que me escribas {label} por mensaje de texto."
            )
            return

        async with self.whatsapp_client.typing(phone):
            reply = await self._collect(phone, parsed_msg.text_content or "", current)
        await self.whatsapp_client.send_text_message(phone, reply)

    async def _collect(self, phone: str, text: str, current: OnboardingData) -> str:
        """Run the onboarding LLM branch, persist any new data and return the reply for the user."""
        try:
            result = await asyncio.to_thread(
                self.groq_client.collect_onboarding_data, text=text, collected=current
            )
        except Exception as e:
            logger.error(f"Onboarding LLM error: {e}")
            return ONBOARDING_ERROR_MESSAGE

        collected = result.collected
        if collected.email and collected.email not in text.lower():
            # An address the user never wrote is a hallucination: ask again instead of storing it
            logger.warning("Discarded an email returned by the LLM that is not present in the user's message.")
            collected = collected.model_copy(update={"email": None})

        merged = current.merged_with(collected)
        if merged != current:
            try:
                await asyncio.to_thread(self.user_repository.save_onboarding, phone, self._sanitized(merged))
            except Exception as e:
                logger.error(f"Could not save onboarding data for {mask_phone(phone)}: {e}")
                return ONBOARDING_ERROR_MESSAGE

        if merged.is_complete:
            logger.info(f"Onboarding completed for {mask_phone(phone)}; awaiting manual approval.")
            return PENDING_APPROVAL_MESSAGE

        return result.reply or (
            f"👋 ¡Hola! Para darte de alta necesito {FIELD_LABELS[merged.missing_fields()[0]]}. Escribímelo por acá."
        )

    @staticmethod
    def _sanitized(data: OnboardingData) -> OnboardingData:
        """Neutralize spreadsheet formula / CSV injection in user-provided text before it is stored."""
        return OnboardingData(
            full_name=DataQualityGate.sanitize_string_for_sheets(data.full_name) or None,
            email=DataQualityGate.sanitize_string_for_sheets(data.email) or None,
        )
