"""Expense processing service orchestrating transcription, extraction, correction, validation, and storage."""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from src.core.exceptions import WhatsAppClientError
from src.core.logging import logger
from src.core.phone import mask_phone
from src.domain.enums import InputTypeEnum
from src.domain.models import DataQualityResult, ExpenseRecord
from src.infrastructure.audio.whisper_client import WhisperClient, audio_filename_for_mime
from src.infrastructure.llm.groq_client import GroqLLMClient
from src.infrastructure.storage.base import ExpenseRepositoryInterface
from src.infrastructure.whatsapp.client import EvolutionAPIClient, decode_base64_media
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services.data_quality import DataQualityGate
from src.services.edit_intent import is_correction_reference, mentions_currency, mentions_date
from src.services.report_intent import is_report_request
from src.services.report_service import ReportService

# The cited message is not one of this user's registered expenses: ask instead of inventing a row
QUOTED_NOT_FOUND_MESSAGE = (
    "❓ No encontré un gasto tuyo en el mensaje que citaste. Para corregir un gasto, respondé citando la "
    "confirmación del bot o el mensaje con el que lo registraste; si es un gasto nuevo, enviámelo sin citar."
)
# The user talks about a stored expense but did not quote it, so there is no way to know which row to change
CORRECTION_NEEDS_QUOTE_MESSAGE = (
    "✏️ Para corregir un gasto necesito que respondas (cites) el mensaje de confirmación del bot —o el "
    "mensaje con el que registraste el gasto—, así sé exactamente qué registro cambiar.\n\n"
    "No registré nada con este mensaje. Si en realidad es un gasto nuevo, enviámelo sin citar "
    '(por ejemplo: "gasté 155 USD en café con débito").'
)
CORRECTION_LOOKUP_ERROR_MESSAGE = (
    "⚠️ No pude consultar el registro que citaste. Por favor intentá de nuevo en unos minutos."
)
CORRECTION_LLM_ERROR_MESSAGE = (
    '⚠️ No pude interpretar la corrección. Indicá qué dato querés cambiar (por ejemplo "eran 15000").'
)
CORRECTION_HINT = "Por favor indicá qué dato querés corregir (monto, fecha, categoría o detalle)."
CORRECTION_PERSISTENCE_ERROR_MESSAGE = (
    "❌ No pude guardar la corrección en la base de datos. Por favor reintentá en unos momentos."
)
CORRECTION_GONE_MESSAGE = "❌ Ese registro ya no existe. Por favor registrá el gasto de nuevo."
CORRECTION_NO_CHANGE_MESSAGE = (
    "🤔 Ese gasto ya está así: no encontré nada para corregir. Si querés cambiar algo, indicá el dato "
    '(por ejemplo "eran 15000" o "era con tarjeta de crédito").'
)

# Google Sheets counts days from this epoch (it reproduces Lotus 1-2-3's leap-year bug)
_SHEETS_SERIAL_EPOCH = date(1899, 12, 30)


def _iso_date(value: object) -> str:
    """Best-effort ISO date from a stored cell: ISO text, local text (dd/mm/yyyy) or a Sheets serial number."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    try:
        serial = float(text)
    except ValueError:
        return text
    # A plausible Sheets date serial (1954..2119); anything else is treated as text
    return (_SHEETS_SERIAL_EPOCH + timedelta(days=int(serial))).isoformat() if 20000 < serial < 80000 else text


@dataclass
class _PipelineOutcome:
    """Reply for the user plus the stored row the reply refers to (so the answer can be linked to it)."""
    reply: Optional[str] = None
    data_quality: Optional[DataQualityResult] = None
    stored_message_id: Optional[str] = None


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
        report_service: Optional[ReportService] = None,
    ):
        self.whisper_client = whisper_client
        self.groq_client = groq_client
        self.repository = repository
        self.whatsapp_client = whatsapp_client
        self.report_service = report_service

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
            outcome = await self._run_pipeline(parsed_msg)

        if outcome.reply:
            # Answering the message itself keeps the thread visible, so the user can quote it back
            reply_id = await self.whatsapp_client.send_reply(
                parsed_msg.sender_phone, outcome.reply, parsed_msg.message_id
            )
            if reply_id and outcome.stored_message_id:
                await self._remember_reply_id(
                    parsed_msg.sender_phone, outcome.stored_message_id, reply_id
                )
        return outcome.data_quality

    async def _remember_reply_id(self, phone: str, message_id: str, reply_id: str) -> None:
        """Link the bot's confirmation to its expense, so quoting that reply later resolves the row."""
        try:
            await asyncio.to_thread(
                self.repository.set_reply_message_id,
                message_id,
                reply_id,
                DataQualityGate.hash_phone_number(phone),
            )
        except Exception as e:
            # Only affects later corrections: the user already got the answer
            logger.warning(f"Could not store the reply id of expense {message_id}: {e}")

    async def _get_audio_bytes(self, parsed_msg: ParsedWhatsAppMessage) -> bytes:
        """Voice note bytes: inline base64 from the webhook when present, otherwise fetched from Evolution."""
        if parsed_msg.media_base64:
            try:
                return decode_base64_media(parsed_msg.media_base64)
            except WhatsAppClientError as e:
                logger.warning(f"Inline audio is unusable ({e}); fetching it from Evolution API instead.")
        return await self.whatsapp_client.download_media(parsed_msg.message_id)

    async def _run_pipeline(self, parsed_msg: ParsedWhatsAppMessage) -> _PipelineOutcome:
        """Transcribe, extract (or correct), validate and persist. Returns the reply and the stored row it refers to."""
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
                return _PipelineOutcome(
                    reply="⚠️ No pudimos procesar el audio enviado. Por favor intenta enviarlo nuevamente o en texto."
                )

        elif parsed_msg.input_type == InputTypeEnum.TEXT:
            raw_text = parsed_msg.text_content or ""

        elif parsed_msg.input_type == InputTypeEnum.IMAGE:
            # MVP handles image caption if provided
            raw_text = parsed_msg.text_content or ""
            if not raw_text:
                return _PipelineOutcome(
                    reply="📸 Recibimos tu ticket. Para el MVP, añade un comentario de texto con el monto o envíalo por audio."
                )

        if not raw_text.strip():
            logger.warning("Empty raw text extracted. Aborting processing.")
            return _PipelineOutcome(
                reply="⚠️ No pudimos entender el mensaje. Por favor intenta nuevamente con un audio más claro o en texto."
            )

        # 2b. Report requests ("quiero mi resumen") are answered with the user's own data, never extracted as expenses
        if self.report_service and is_report_request(raw_text):
            logger.info(f"Report requested by {mask_phone(parsed_msg.sender_phone)}.")
            return _PipelineOutcome(reply=await self.report_service.handle(parsed_msg.sender_phone))

        # 2c. A quoted message means the user is correcting an expense already registered, never a new one
        if parsed_msg.quoted_message_id:
            return await self._correct_expense(parsed_msg, raw_text)

        # 2d. A correction without a quote cannot be resolved to a row: ask for it instead of failing on extraction
        if is_correction_reference(raw_text):
            logger.info(
                f"Correction without a quoted message from {mask_phone(parsed_msg.sender_phone)}; asking for the quote."
            )
            return _PipelineOutcome(reply=CORRECTION_NEEDS_QUOTE_MESSAGE)

        # 3. Entity Extraction via LLM (Llama 3 on Groq)
        try:
            llm_result = await asyncio.to_thread(
                self.groq_client.extract_expense_entities,
                text=raw_text,
                reference_date=date.today(),
            )
        except Exception as e:
            logger.error(f"LLM extraction error: {e}")
            return _PipelineOutcome(
                reply="⚠️ Ocurrió un error analizando los datos del gasto. Por favor intenta más tarde."
            )

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
            return _PipelineOutcome(
                reply=f"⚠️ No pude registrar tu gasto con certeza suficiente:\n- {error_reasons}\n\nPor favor reenvía el mensaje aclarando el monto y concepto.",
                data_quality=dq_result,
            )

        # 6. Persistence in Repository (Sheets or Postgres)
        record = dq_result.sanitized_record
        if not record:
            return _PipelineOutcome(data_quality=dq_result)

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
            return _PipelineOutcome(
                reply=confirmation_msg,
                data_quality=dq_result,
                stored_message_id=record.message_id,
            )
        except Exception as e:
            logger.error(f"Persistence error: {e}")
            return _PipelineOutcome(
                reply="❌ Hubo un inconveniente al guardar tu gasto en la base de datos. Por favor reintenta en unos momentos.",
                data_quality=dq_result,
            )

    async def _correct_expense(
        self, parsed_msg: ParsedWhatsAppMessage, raw_text: str
    ) -> _PipelineOutcome:
        """Apply the user's correction to the expense cited in the message. Never inserts a new row."""
        cited_id = parsed_msg.quoted_message_id or ""
        user_hash = DataQualityGate.hash_phone_number(parsed_msg.sender_phone)

        try:
            original = await asyncio.to_thread(
                self.repository.find_expense_by_message_id, cited_id, user_hash
            )
        except Exception as e:
            logger.error(f"Could not look up the cited expense {cited_id}: {e}")
            return _PipelineOutcome(reply=CORRECTION_LOOKUP_ERROR_MESSAGE)

        if not original:
            logger.info(f"Message {cited_id} is not one of this user's expenses; asking for clarification.")
            return _PipelineOutcome(reply=QUOTED_NOT_FOUND_MESSAGE)

        message_id = str(original.get("message_id") or "").strip()
        if not message_id:
            logger.warning(f"The cited row has no message_id; correction of {cited_id} aborted.")
            return _PipelineOutcome(reply=QUOTED_NOT_FOUND_MESSAGE)

        try:
            llm_result = await asyncio.to_thread(
                self.groq_client.correct_expense_entities,
                text=raw_text,
                original=self._as_contract_transaction(original),
                reference_date=date.today(),
            )
        except Exception as e:
            logger.error(f"LLM correction error: {e}")
            return _PipelineOutcome(reply=CORRECTION_LLM_ERROR_MESSAGE)

        # Same gate as a new expense, but keeping the original message id (the row identity)
        dq_result = DataQualityGate.validate_and_sanitize(
            llm_response=llm_result,
            message_id=message_id,
            user_phone=parsed_msg.sender_phone,
            input_type=parsed_msg.input_type,
        )
        record = dq_result.sanitized_record

        if not dq_result.is_valid or not record:
            error_reasons = "\n- ".join(dq_result.errors)
            logger.warning(f"Correction validation failed for message {message_id}: {error_reasons}")
            return _PipelineOutcome(
                reply=f"⚠️ No pude aplicar la corrección con certeza suficiente:\n- {error_reasons}\n\n{CORRECTION_HINT}",
                data_quality=dq_result,
            )

        # Fields the user did not mention must stay as stored, whatever the model returns
        record = self._keep_unmentioned_fields(original, record, raw_text)

        if not self._correction_changes_anything(original, record):
            logger.info(f"Correction of {message_id} would not change any field; nothing was written.")
            return _PipelineOutcome(reply=CORRECTION_NO_CHANGE_MESSAGE, data_quality=dq_result)

        try:
            updated = await asyncio.to_thread(self.repository.update_expense, record)
        except Exception as e:
            logger.error(f"Correction persistence error: {e}")
            return _PipelineOutcome(reply=CORRECTION_PERSISTENCE_ERROR_MESSAGE, data_quality=dq_result)

        if not updated:
            logger.warning(f"Expense {message_id} could not be updated: the row is gone.")
            return _PipelineOutcome(reply=CORRECTION_GONE_MESSAGE, data_quality=dq_result)

        logger.info(f"Expense {message_id} corrected by {mask_phone(parsed_msg.sender_phone)}.")
        return _PipelineOutcome(
            reply=self._correction_confirmation(original, record),
            data_quality=dq_result,
            stored_message_id=message_id,
        )

    @classmethod
    def _correction_changes_anything(cls, original: dict, record: ExpenseRecord) -> bool:
        """False when the corrected record is identical to the stored one: nothing to write, nothing to confirm."""
        current = cls._as_contract_transaction(original)
        return any(
            (
                current["transaction_date"] != record.transaction_date.isoformat(),
                round(float(current["amount"]), 2) != round(record.amount, 2),
                str(current["currency"]).upper() != record.currency.upper(),
                current["category"] != record.category,
                current["subcategory"] != record.subcategory,
                current["payment_method"] != record.payment_method,
                current["description"] != record.description,
            )
        )

    @classmethod
    def _keep_unmentioned_fields(cls, original: dict, record: ExpenseRecord, text: str) -> ExpenseRecord:
        """Restore the currency and the date when the correction text does not mention them.

        The model must only change what the user said: without this guard a bare "eran 155" could
        silently turn 15 USD into 155 ARS (the default currency of the extraction prompt).
        """
        current = cls._as_contract_transaction(original)
        updates: dict = {}
        if not mentions_currency(text):
            stored_currency = str(current["currency"]).upper()
            if stored_currency and stored_currency != record.currency.upper():
                logger.info(f"Correction kept the stored currency {stored_currency} (not mentioned).")
                updates["currency"] = stored_currency
        if not mentions_date(text):
            stored_date = current["transaction_date"]
            if stored_date and stored_date != record.transaction_date.isoformat():
                logger.info(f"Correction kept the stored date {stored_date} (not mentioned).")
                updates["transaction_date"] = date.fromisoformat(stored_date)
        return record.model_copy(update=updates) if updates else record

    @staticmethod
    def _as_contract_transaction(stored: dict) -> dict:
        """Stored row expressed with the LLM contract's keys, so the model only returns it updated."""

        def cell(name: str) -> str:
            return str(stored.get(name) or "").strip().lstrip("'")

        try:
            amount = round(float(cell("monto").replace(",", ".")), 2)
        except ValueError:
            amount = 0.0

        return {
            "transaction_date": _iso_date(cell("fecha_gasto")),
            "transaction_time": None,
            "amount": amount,
            "currency": cell("moneda") or "ARS",
            "category": cell("categoria") or "Otros",
            "subcategory": cell("subcategoria") or "Otros",
            "merchant_name": None,
            "payment_method": cell("medio_de_pago") or "No Especificado",
            "description": cell("descripcion") or "Gasto registrado",
        }

    @staticmethod
    def _correction_confirmation(original: dict, record: ExpenseRecord) -> str:
        """WhatsApp summary of the updated expense, marking the fields that actually changed."""

        def stored(name: str) -> str:
            return str(original.get(name) or "").strip().lstrip("'")

        def change(old: str, new: str) -> str:
            return f" (antes {old})" if old and old != new else ""

        try:
            old_amount: Optional[float] = round(float(stored("monto").replace(",", ".")), 2)
        except ValueError:
            old_amount = None

        new_date = record.transaction_date.isoformat()
        new_category = f"{record.category} ({record.subcategory})"
        old_category = f"{stored('categoria')} ({stored('subcategoria')})"
        amount = f"{record.amount:,.2f} {record.currency}"
        if old_amount is not None and old_amount != round(record.amount, 2):
            amount += f" (antes {old_amount:,.2f} {record.currency})"

        return (
            "✏️ *Registro modificado*\n\n"
            f"💰 *Monto:* {amount}\n"
            f"📅 *Fecha:* {new_date}{change(_iso_date(stored('fecha_gasto')), new_date)}\n"
            f"📂 *Categoría:* {new_category}{change(old_category, new_category)}\n"
            f"📝 *Detalle:* {record.description}{change(stored('descripcion'), record.description)}\n"
            f"💳 *Método:* {record.payment_method}{change(stored('medio_de_pago'), record.payment_method)}"
        )
