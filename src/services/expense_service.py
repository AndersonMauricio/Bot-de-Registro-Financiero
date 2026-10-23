"""Expense processing service orchestrating transcription, extraction, validation, and storage."""

import asyncio
from datetime import date
from typing import Optional
from src.core.exceptions import WhatsAppClientError
from src.core.logging import logger
from src.core.phone import mask_phone
from src.domain.enums import InputTypeEnum
from src.domain.models import DataQualityResult
from src.infrastructure.audio.whisper_client import WhisperClient, audio_filename_for_mime
from src.infrastructure.llm.groq_client import GroqLLMClient
from src.infrastructure.storage.base import ExpenseRepositoryInterface
from src.infrastructure.whatsapp.client import EvolutionAPIClient, decode_base64_media
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services.data_quality import DataQualityGate


class ExpenseProcessingService:
    """Core domain service for orchestrating WhatsApp expense ingestion.

    Only reached for approved users: access control lives in MessageRouter, not here.
    Blocking SDK calls (Whisper, Groq, repositories) run in worker threads so the event loop stays free
    to send the "typing..." presence while they work.
    """

    def __init__(
        self,
        whisper_client: WhisperClient,
        groq_client: GroqLLMClient,
        repository: ExpenseRepositoryInterface,
        whatsapp_client: EvolutionAPIClient,
    ):
        self.whisper_client = whisper_client
        self.groq_client = groq_client
        self.repository = repository
        self.whatsapp_client = whatsapp_client

    async def process_incoming_message(
        self, parsed_msg: ParsedWhatsAppMessage
    ) -> Optional[DataQualityResult]:
        """Execute end-to-end ingestion pipeline for an incoming WhatsApp message."""
        logger.info(
            f"Processing message {parsed_msg.message_id} from {mask_phone(parsed_msg.sender_phone)} [{parsed_msg.input_type.value}]"
        )

        # 1. Idempotency Check
        if await asyncio.to_thread(self.repository.is_message_processed, parsed_msg.message_id):
            logger.info(f"Message {parsed_msg.message_id} was already processed. Skipping duplicate.")
            return None

        # Show "typing..." while the heavy work runs, and stop it before the reply goes out
        async with self.whatsapp_client.typing(parsed_msg.sender_phone):
            reply, dq_result = await self._run_pipeline(parsed_msg)

        if reply:
            await self.whatsapp_client.send_text_message(parsed_msg.sender_phone, reply)
        return dq_result

    async def _get_audio_bytes(self, parsed_msg: ParsedWhatsAppMessage) -> bytes:
        """Voice note bytes: inline base64 from the webhook when present, otherwise fetched from Evolution."""
        if parsed_msg.media_base64:
            try:
                return decode_base64_media(parsed_msg.media_base64)
            except WhatsAppClientError as e:
                logger.warning(f"Inline audio is unusable ({e}); fetching it from Evolution API instead.")
        return await self.whatsapp_client.download_media(parsed_msg.message_id)

    async def _run_pipeline(
        self, parsed_msg: ParsedWhatsAppMessage
    ) -> tuple[Optional[str], Optional[DataQualityResult]]:
        """Transcribe, extract, validate and persist. Returns (reply for the user, data quality result)."""
        # 2. Extract or Transcribe Text
        raw_text: str = ""

        if parsed_msg.input_type == InputTypeEnum.AUDIO:
            try:
                audio_bytes = await self._get_audio_bytes(parsed_msg)
                raw_text = await asyncio.to_thread(
                    self.whisper_client.transcribe_audio_bytes,
                    audio_bytes,
                    filename=audio_filename_for_mime(parsed_msg.mime_type),
                )
            except Exception as e:
                logger.error(f"Failed to transcribe audio message: {e}")
                return (
                    "⚠️ No pudimos procesar el audio enviado. Por favor intenta enviarlo nuevamente o en texto.",
                    None,
                )

        elif parsed_msg.input_type == InputTypeEnum.TEXT:
            raw_text = parsed_msg.text_content or ""

        elif parsed_msg.input_type == InputTypeEnum.IMAGE:
            # MVP handles image caption if provided
            raw_text = parsed_msg.text_content or ""
            if not raw_text:
                return (
                    "📸 Recibimos tu ticket. Para el MVP, añade un comentario de texto con el monto o envíalo por audio.",
                    None,
                )

        if not raw_text.strip():
            logger.warning("Empty raw text extracted. Aborting processing.")
            return (
                "⚠️ No pudimos entender el mensaje. Por favor intenta nuevamente con un audio más claro o en texto.",
                None,
            )

        # 3. Entity Extraction via LLM (Llama 3 on Groq)
        try:
            llm_result = await asyncio.to_thread(
                self.groq_client.extract_expense_entities,
                text=raw_text,
                reference_date=date.today(),
            )
        except Exception as e:
            logger.error(f"LLM extraction error: {e}")
            return ("⚠️ Ocurrió un error analizando los datos del gasto. Por favor intenta más tarde.", None)

        # 4. Data Quality Gate & Sanitization
        dq_result = DataQualityGate.validate_and_sanitize(
            llm_response=llm_result,
            message_id=parsed_msg.message_id,
            user_phone=parsed_msg.sender_phone,
            input_type=parsed_msg.input_type,
        )

        # 5. Route Result
        if not dq_result.is_valid:
            error_reasons = "\n- ".join(dq_result.errors)
            logger.warning(f"Data quality validation failed for message {parsed_msg.message_id}: {error_reasons}")
            return (
                f"⚠️ No pude registrar tu gasto con certeza suficiente:\n- {error_reasons}\n\nPor favor reenvía el mensaje aclarando el monto y concepto.",
                dq_result,
            )

        # 6. Persistence in Repository (Sheets or Postgres)
        record = dq_result.sanitized_record
        if not record:
            return None, dq_result

        try:
            await asyncio.to_thread(self.repository.save_expense, record)
            # 7. Notify Success to User
            confirmation_msg = (
                f"✅ *Gasto Registrado con Éxito*\n\n"
                f"💰 *Monto:* {record.amount:,.2f} {record.currency}\n"
                f"📂 *Categoría:* {record.category} ({record.subcategory})\n"
                f"📝 *Detalle:* {record.description}\n"
                f"💳 *Método:* {record.payment_method}\n"
                f"📅 *Fecha:* {record.transaction_date.isoformat()}"
            )
            return confirmation_msg, dq_result
        except Exception as e:
            logger.error(f"Persistence error: {e}")
            return (
                "❌ Hubo un inconveniente al guardar tu gasto en la base de datos. Por favor reintenta en unos momentos.",
                dq_result,
            )
