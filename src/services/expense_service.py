"""Expense processing service orchestrating transcription, extraction, validation, and storage."""

from datetime import date
from typing import Optional
from src.core.exceptions import BotWppException
from src.core.logging import logger
from src.domain.enums import InputTypeEnum
from src.domain.models import DataQualityResult
from src.infrastructure.audio.whisper_client import WhisperClient
from src.infrastructure.llm.groq_client import GroqLLMClient
from src.infrastructure.storage.base import ExpenseRepositoryInterface
from src.infrastructure.whatsapp.client import WhatsAppClient
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services.data_quality import DataQualityGate


class ExpenseProcessingService:
    """Core domain service for orchestrating WhatsApp expense ingestion."""

    def __init__(
        self,
        whisper_client: WhisperClient,
        groq_client: GroqLLMClient,
        repository: ExpenseRepositoryInterface,
        whatsapp_client: WhatsAppClient,
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
            f"Processing message {parsed_msg.message_id} from {parsed_msg.sender_phone} [{parsed_msg.input_type.value}]"
        )

        # 1. Idempotency Check
        if self.repository.is_message_processed(parsed_msg.message_id):
            logger.info(f"Message {parsed_msg.message_id} was already processed. Skipping duplicate.")
            return None

        # 2. Extract or Transcribe Text
        raw_text: str = ""

        if parsed_msg.input_type == InputTypeEnum.AUDIO:
            if not parsed_msg.media_id:
                logger.error("Audio message received without media_id.")
                return None
            try:
                audio_bytes = await self.whatsapp_client.download_media(parsed_msg.media_id)
                raw_text = self.whisper_client.transcribe_audio_bytes(audio_bytes)
            except Exception as e:
                logger.error(f"Failed to transcribe audio message: {e}")
                await self.whatsapp_client.send_text_message(
                    parsed_msg.sender_phone,
                    "⚠️ No pudimos procesar el audio enviado. Por favor intenta enviarlo nuevamente o en texto.",
                )
                return None

        elif parsed_msg.input_type == InputTypeEnum.TEXT:
            raw_text = parsed_msg.text_content or ""

        elif parsed_msg.input_type == InputTypeEnum.IMAGE:
            # MVP handles image caption if provided
            raw_text = parsed_msg.text_content or ""
            if not raw_text:
                await self.whatsapp_client.send_text_message(
                    parsed_msg.sender_phone,
                    "📸 Recibimos tu ticket. Para el MVP, añade un comentario de texto con el monto o envíalo por audio.",
                )
                return None

        if not raw_text.strip():
            logger.warning("Empty raw text extracted. Aborting processing.")
            return None

        # 3. Entity Extraction via LLM (Llama 3 on Groq)
        try:
            llm_result = self.groq_client.extract_expense_entities(
                text=raw_text,
                reference_date=date.today(),
            )
        except Exception as e:
            logger.error(f"LLM extraction error: {e}")
            await self.whatsapp_client.send_text_message(
                parsed_msg.sender_phone,
                "⚠️ Ocurrió un error analizando los datos del gasto. Por favor intenta más tarde.",
            )
            return None

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
            await self.whatsapp_client.send_text_message(
                parsed_msg.sender_phone,
                f"⚠️ No pude registrar tu gasto con certeza suficiente:\n- {error_reasons}\n\nPor favor reenvía el mensaje aclarando el monto y concepto.",
            )
            return dq_result

        # 6. Persistence in Repository (Sheets or Postgres)
        record = dq_result.sanitized_record
        if not record:
            return dq_result

        try:
            self.repository.save_expense(record)
            # 7. Notify Success to User
            confirmation_msg = (
                f"✅ *Gasto Registrado con Éxito*\n\n"
                f"💰 *Monto:* {record.amount:,.2f} {record.currency}\n"
                f"📂 *Categoría:* {record.category} ({record.subcategory})\n"
                f"📝 *Detalle:* {record.description}\n"
                f"💳 *Método:* {record.payment_method}\n"
                f"📅 *Fecha:* {record.transaction_date.isoformat()}"
            )
            await self.whatsapp_client.send_text_message(
                parsed_msg.sender_phone, confirmation_msg
            )
        except Exception as e:
            logger.error(f"Persistence error: {e}")
            await self.whatsapp_client.send_text_message(
                parsed_msg.sender_phone,
                "❌ Hubo un inconveniente al guardar tu gasto en la base de datos. Por favor reintenta en unos momentos.",
            )

        return dq_result
