"""Expense processing service orchestrating transcription, extraction, correction, validation, and storage."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional
from src.core.async_utils import run_blocking
from src.core.config import settings
from src.core.exceptions import ServiceTimeoutError, WhatsAppClientError
from src.core.logging import logger
from src.core.phone import mask_phone
from src.domain.enums import InputTypeEnum
from src.domain.models import DataQualityResult, ExpenseRecord
from src.infrastructure.audio.whisper_client import WhisperClient, audio_filename_for_mime
from src.infrastructure.llm.groq_client import GroqLLMClient
from src.infrastructure.storage.base import ExpenseRepositoryInterface
from src.infrastructure.whatsapp.client import EvolutionAPIClient, decode_base64_media
from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
from src.services.batch_selection import PendingSelection, PendingSelectionStore, describe, numbered
from src.services.data_quality import DataQualityGate
from src.services.edit_intent import is_correction_reference, mentions_currency, mentions_date, parse_selection
from src.services.report_intent import is_report_request
from src.services.report_service import ReportService

# External services failing: the user must know the expense was NOT saved, so they can retry
AI_ERROR_MESSAGE = (
    "⚠️ Ocurrió un error en el servidor de inteligencia artificial. Tu gasto no fue guardado. "
    "Por favor, intentá de nuevo en unos minutos."
)
AI_TIMEOUT_MESSAGE = (
    "⚠️ El servidor de inteligencia artificial tardó demasiado en responder. Tu gasto no fue guardado. "
    "Por favor, intentá de nuevo en unos minutos."
)
AUDIO_ERROR_MESSAGE = (
    "⚠️ No pudimos procesar el audio enviado. No se registró nada: intentá enviarlo nuevamente o en texto."
)
STORAGE_ERROR_MESSAGE = (
    "⚠️ No pude guardar tu registro en la base de datos en este momento. No se guardó nada. "
    "Conservá el ticket o intentá de nuevo en un rato."
)
STORAGE_TIMEOUT_MESSAGE = (
    "⚠️ La base de datos no respondió a tiempo: puede que tu gasto no se haya guardado. "
    'Escribí "quiero mi resumen" para verificarlo antes de reenviarlo.'
)
EMPTY_TEXT_MESSAGE = (
    "⚠️ No pudimos entender el mensaje. No se registró nada: probá con un audio más claro o escribí el gasto."
)

# Edit mode (quoting a message)
QUOTED_NOT_FOUND_MESSAGE = (
    "❓ No encontré un gasto tuyo en el mensaje que citaste. Para corregir un gasto, respondé citando la "
    "confirmación del bot o el mensaje con el que lo registraste; si es un gasto nuevo, enviámelo sin citar."
)
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
    "❌ No pude guardar la corrección en la base de datos. No se modificó nada. Por favor reintentá en unos momentos."
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


def _cell(stored: dict, name: str) -> str:
    """Text of a stored value, without the apostrophe that keeps identifiers as text."""
    return str(stored.get(name) or "").strip().lstrip("'")


@dataclass
class _PipelineOutcome:
    """Reply for the user plus the stored rows it refers to (so the answer can be linked back to them)."""
    reply: Optional[str] = None
    data_quality: Optional[DataQualityResult] = None
    stored_message_ids: list[str] = field(default_factory=list)
    pending_selection: Optional[PendingSelection] = None


class ExpenseProcessingService:
    """Core domain service for orchestrating WhatsApp expense ingestion.

    Only reached for approved users: access control lives in MessageRouter, not here.
    Every blocking SDK call (Whisper, Groq, repositories) runs in a worker thread with a time budget,
    so a hung provider can never leave the user without an answer.
    """

    def __init__(
        self,
        whisper_client: WhisperClient,
        groq_client: GroqLLMClient,
        repository: ExpenseRepositoryInterface,
        whatsapp_client: EvolutionAPIClient,
        report_service: Optional[ReportService] = None,
        pending_selections: Optional[PendingSelectionStore] = None,
    ):
        self.whisper_client = whisper_client
        self.groq_client = groq_client
        self.repository = repository
        self.whatsapp_client = whatsapp_client
        self.report_service = report_service
        self._pending = pending_selections or PendingSelectionStore()

    async def process_incoming_message(
        self, parsed_msg: ParsedWhatsAppMessage
    ) -> Optional[DataQualityResult]:
        """Execute end-to-end ingestion pipeline for an incoming WhatsApp message."""
        logger.info(
            f"Processing message {parsed_msg.message_id} from {mask_phone(parsed_msg.sender_phone)} [{parsed_msg.input_type.value}]"
        )

        # 1. Idempotency Check
        try:
            already = await run_blocking(
                self.repository.is_message_processed,
                parsed_msg.message_id,
                timeout=settings.STORAGE_TIMEOUT_SECONDS,
                label="idempotency check",
            )
        except ServiceTimeoutError:
            # A slow read must never block ingestion: the router's duplicate cache still protects us
            logger.warning(f"Could not check idempotency for {parsed_msg.message_id} in time; processing anyway.")
            already = False
        if already:
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
            if reply_id and outcome.pending_selection:
                # The numbered list becomes the message to answer for the follow-up
                outcome.pending_selection.prompt_message_id = reply_id
            if reply_id and outcome.stored_message_ids:
                await self._remember_reply_ids(
                    parsed_msg.sender_phone, outcome.stored_message_ids, reply_id
                )
        return outcome.data_quality

    async def _remember_reply_ids(self, phone: str, message_ids: list[str], reply_id: str) -> None:
        """Link the bot's confirmation to its rows, so quoting that reply later resolves them."""
        try:
            await run_blocking(
                self.repository.set_reply_message_ids,
                message_ids,
                reply_id,
                DataQualityGate.hash_phone_number(phone),
                timeout=settings.STORAGE_TIMEOUT_SECONDS,
                label="store reply ids",
            )
        except Exception as e:
            # Only affects later corrections: the user already got the answer
            logger.warning(f"Could not store the reply id of {len(message_ids)} expense(s): {e}")

    async def _get_audio_bytes(self, parsed_msg: ParsedWhatsAppMessage) -> bytes:
        """Voice note bytes: inline base64 from the webhook when present, otherwise fetched from Evolution."""
        if parsed_msg.media_base64:
            try:
                return decode_base64_media(parsed_msg.media_base64)
            except WhatsAppClientError as e:
                logger.warning(f"Inline audio is unusable ({e}); fetching it from Evolution API instead.")
        return await self.whatsapp_client.download_media(parsed_msg.message_id)

    async def _run_pipeline(self, parsed_msg: ParsedWhatsAppMessage) -> _PipelineOutcome:
        """Transcribe, extract (or correct), validate and persist. Returns the reply for the user."""
        # 2. Extract or Transcribe Text
        raw_text: str = ""

        if parsed_msg.input_type == InputTypeEnum.AUDIO:
            try:
                audio_bytes = await self._get_audio_bytes(parsed_msg)
                raw_text = await run_blocking(
                    self.whisper_client.transcribe_audio_bytes,
                    audio_bytes,
                    filename=audio_filename_for_mime(parsed_msg.mime_type),
                    timeout=settings.GROQ_TIMEOUT_SECONDS,
                    label="audio transcription",
                )
            except ServiceTimeoutError:
                logger.error("Audio transcription timed out.")
                return _PipelineOutcome(reply=AUDIO_ERROR_MESSAGE)
            except Exception as e:
                logger.error(f"Failed to transcribe audio message: {e}")
                return _PipelineOutcome(reply=AUDIO_ERROR_MESSAGE)

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
            return _PipelineOutcome(reply=EMPTY_TEXT_MESSAGE)

        # 2b. Report requests ("quiero mi resumen") are answered with the user's own data, never extracted as expenses
        if self.report_service and is_report_request(raw_text):
            logger.info(f"Report requested by {mask_phone(parsed_msg.sender_phone)}.")
            return _PipelineOutcome(reply=await self.report_service.handle(parsed_msg.sender_phone))

        # 2c. A pending "which expense of the batch?" list takes precedence over a new expense
        selection_outcome = await self._handle_pending_selection(parsed_msg, raw_text)
        if selection_outcome is not None:
            return selection_outcome

        # 2d. A quoted message means the user is correcting an expense already registered, never a new one
        if parsed_msg.quoted_message_id:
            return await self._correct_expense(parsed_msg, raw_text)

        # 2e. A correction without a quote cannot be resolved to a row: ask for it instead of failing on extraction
        if is_correction_reference(raw_text):
            logger.info(
                f"Correction without a quoted message from {mask_phone(parsed_msg.sender_phone)}; asking for the quote."
            )
            return _PipelineOutcome(reply=CORRECTION_NEEDS_QUOTE_MESSAGE)

        # 3. Entity Extraction via LLM (one or several expenses)
        try:
            llm_result = await run_blocking(
                self.groq_client.extract_expense_entities,
                text=raw_text,
                reference_date=date.today(),
                timeout=settings.GROQ_TIMEOUT_SECONDS,
                label="expense extraction",
            )
        except ServiceTimeoutError:
            logger.error(f"LLM extraction timed out for message {parsed_msg.message_id}.")
            return _PipelineOutcome(reply=AI_TIMEOUT_MESSAGE)
        except Exception as e:
            logger.error(f"LLM extraction error: {e}")
            return _PipelineOutcome(reply=AI_ERROR_MESSAGE)

        if llm_result is None:
            logger.error(f"LLM returned no extraction for message {parsed_msg.message_id}.")
            return _PipelineOutcome(reply=AI_ERROR_MESSAGE)

        # 4-6. Validate every transaction and persist the valid ones in a single write
        return await self._validate_and_store(parsed_msg, llm_result)

    async def _validate_and_store(self, parsed_msg: ParsedWhatsAppMessage, llm_result) -> _PipelineOutcome:
        """Run the data quality gate per transaction and store the valid ones as one batch."""
        results = DataQualityGate.validate_transactions(
            llm_response=llm_result,
            message_id=parsed_msg.message_id,
            user_phone=parsed_msg.sender_phone,
            input_type=parsed_msg.input_type,
        )
        summary = self._summarize(results)
        records = [result.sanitized_record for result in results if result.sanitized_record]
        failures = [result for result in results if not result.is_valid]

        if not records:
            logger.warning(
                f"Data quality validation failed for message {parsed_msg.message_id}: "
                f"{'; '.join(error for result in failures for error in result.errors)}"
            )
            return _PipelineOutcome(reply=self._validation_failure_message(results), data_quality=summary)

        try:
            await run_blocking(
                self.repository.save_expenses,
                records,
                timeout=settings.STORAGE_TIMEOUT_SECONDS,
                label="save expenses",
            )
        except ServiceTimeoutError:
            return _PipelineOutcome(reply=STORAGE_TIMEOUT_MESSAGE, data_quality=summary)
        except Exception as e:
            logger.error(f"Persistence error: {e}")
            return _PipelineOutcome(
                reply=STORAGE_ERROR_MESSAGE if len(results) == 1 else self._batch_storage_failure(len(results)),
                data_quality=summary,
            )

        return _PipelineOutcome(
            reply=self._confirmation(records, failures, total=len(results)),
            data_quality=summary,
            stored_message_ids=[record.message_id for record in records],
        )

    @staticmethod
    def _summarize(results: list[DataQualityResult]) -> DataQualityResult:
        """Single result for one expense; an aggregate (without a record) for a batch."""
        if len(results) == 1:
            return results[0]
        return DataQualityResult(
            is_valid=all(result.is_valid for result in results),
            confidence_score=max(result.confidence_score for result in results),
            errors=[error for result in results for error in result.errors],
            warnings=[warning for result in results for warning in result.warnings],
            sanitized_record=None,
        )

    @staticmethod
    def _validation_failure_message(results: list[DataQualityResult]) -> str:
        """Why nothing was registered, one entry per detected expense."""
        if len(results) == 1:
            reasons = "\n- ".join(results[0].errors)
            return (
                f"⚠️ No pude registrar tu gasto con certeza suficiente:\n- {reasons}\n\n"
                f"Por favor reenvía el mensaje aclarando el monto y concepto."
            )
        lines = "\n".join(
            f"{index}. " + "; ".join(result.errors) for index, result in enumerate(results, start=1)
        )
        return (
            f"⚠️ No pude registrar ninguno de los {len(results)} gastos que detecté:\n"
            f"{lines}\n\n"
            f"Reenviá el mensaje aclarando monto y concepto, o mandá cada gasto por separado."
        )

    @staticmethod
    def _batch_storage_failure(total: int) -> str:
        return (
            f"⚠️ No pude guardar ninguno de los {total} gastos en la base de datos. No se guardó nada. "
            f"Conservá el ticket o intentá de nuevo en un rato."
        )

    @staticmethod
    def _confirmation(
        records: list[ExpenseRecord],
        failures: list[DataQualityResult],
        total: int,
    ) -> str:
        """WhatsApp confirmation: the usual single-expense text, or a numbered list for a batch."""
        if len(records) == 1 and not failures:
            record = records[0]
            return (
                f"✅ *Gasto Registrado con Éxito*\n\n"
                f"💰 *Monto:* {record.amount:,.2f} {record.currency}\n"
                f"📂 *Categoría:* {record.category} ({record.subcategory})\n"
                f"📝 *Detalle:* {record.description}\n"
                f"💳 *Método:* {record.payment_method}\n"
                f"📅 *Fecha:* {record.transaction_date.isoformat()}"
            )

        if failures:
            header = f"✅ Se registraron {len(records)} de {total} gastos:"
        elif len(records) > 1:
            header = f"✅ Se registraron {len(records)} gastos:"
        else:
            header = "✅ Se registró 1 gasto:"

        def label(index: int, record: ExpenseRecord) -> str:
            line = f"{index}. {record.category} ({record.subcategory}) — {record.amount:,.2f} {record.currency}"
            # The detail is only worth appending when it says something the subcategory does not
            if record.description.strip().casefold() not in record.subcategory.strip().casefold():
                line += f" · {record.description}"
            return line

        items = "\n".join(label(index, record) for index, record in enumerate(records, start=1))

        footer_parts = []
        dates = {record.transaction_date.isoformat() for record in records}
        methods = {record.payment_method for record in records}
        if len(dates) == 1:
            footer_parts.append(f"📅 {dates.pop()}")
        if len(methods) == 1:
            footer_parts.append(f"💳 {methods.pop()}")

        message = f"{header}\n\n{items}"
        if footer_parts:
            message += "\n\n" + " · ".join(footer_parts)
        if failures:
            reasons = "\n".join(
                f"{index}. " + "; ".join(result.errors)
                for index, result in enumerate(failures, start=len(records) + 1)
            )
            message += (
                f"\n\n⚠️ No pude registrar:\n{reasons}\n\n"
                f"Reenviá solo lo que falta, aclarando monto y concepto."
            )
        return message

    # ------------------------------------------------------------------ batch row selection
    @staticmethod
    def _user_key(phone: str) -> str:
        return DataQualityGate.hash_phone_number(phone)

    async def _handle_pending_selection(
        self, parsed_msg: ParsedWhatsAppMessage, raw_text: str
    ) -> Optional[_PipelineOutcome]:
        """Resolve "el 2 eran 6000" when the user was asked which expense of a batch to correct."""
        user_key = self._user_key(parsed_msg.sender_phone)
        pending = self._pending.get(user_key)
        if pending is None:
            return None

        quoted = parsed_msg.quoted_message_id
        if quoted and quoted != pending.prompt_message_id:
            # The user quoted something else: they moved on
            self._pending.clear(user_key)
            return None

        index, remaining = parse_selection(raw_text)

        if index is None:
            if pending.awaiting_value is not None:
                # The bot already asked "¿qué querés corregir del gasto 2?": this message is the new value
                candidate = pending.candidates[pending.awaiting_value - 1]
                self._pending.clear(user_key)
                return await self._apply_correction(parsed_msg, raw_text.strip(), candidate)
            self._pending.clear(user_key)
            return None

        if not 1 <= index <= len(pending.candidates):
            return _PipelineOutcome(
                reply=(
                    f"⚠️ No tengo una opción {index}. Elegí un número del 1 al {len(pending.candidates)}:\n\n"
                    f"{numbered(pending.candidates)}"
                ),
                pending_selection=pending,
            )

        candidate = pending.candidates[index - 1]
        if not remaining:
            pending.awaiting_value = index
            return _PipelineOutcome(
                reply=(
                    f"👉 ¿Qué querés corregir del gasto {index} ({describe(candidate)})? "
                    f'Por ejemplo: "eran 6000".'
                ),
                pending_selection=pending,
            )

        self._pending.clear(user_key)
        return await self._apply_correction(parsed_msg, remaining, candidate)

    # ------------------------------------------------------------------ edit mode
    async def _correct_expense(
        self, parsed_msg: ParsedWhatsAppMessage, raw_text: str
    ) -> _PipelineOutcome:
        """Apply the user's correction to the expense(s) cited in the message. Never inserts a new row."""
        cited_id = parsed_msg.quoted_message_id or ""
        user_key = self._user_key(parsed_msg.sender_phone)

        try:
            matches = await run_blocking(
                self.repository.find_expenses_by_message_id,
                cited_id,
                user_key,
                timeout=settings.STORAGE_TIMEOUT_SECONDS,
                label="find cited expense",
            )
        except Exception as e:
            logger.error(f"Could not look up the cited expense {cited_id}: {e}")
            return _PipelineOutcome(reply=CORRECTION_LOOKUP_ERROR_MESSAGE)

        if not matches:
            logger.info(f"Message {cited_id} is not one of this user's expenses; asking for clarification.")
            return _PipelineOutcome(reply=QUOTED_NOT_FOUND_MESSAGE)

        if len(matches) > 1:
            # One confirmation covers the whole batch: the user must name the expense, in this message
            # ("el 2 eran 6000") or in the next one after the numbered list
            index, remaining = parse_selection(raw_text)
            if index is not None and 1 <= index <= len(matches):
                if remaining:
                    return await self._apply_correction(parsed_msg, remaining, matches[index - 1])
                pending = self._pending.put(user_key, matches)
                pending.awaiting_value = index
                return _PipelineOutcome(
                    reply=(
                        f"👉 ¿Qué querés corregir del gasto {index} ({describe(matches[index - 1])})? "
                        f'Por ejemplo: "eran 6000".'
                    ),
                    pending_selection=pending,
                )
            pending = self._pending.put(user_key, matches)
            return _PipelineOutcome(
                reply=(
                    f"🔎 Esa confirmación tiene {len(matches)} gastos. ¿Cuál querés corregir?\n\n"
                    f"{numbered(matches)}\n\n"
                    f'Respondé, por ejemplo: "el 2 eran 25000".'
                ),
                pending_selection=pending,
            )

        return await self._apply_correction(parsed_msg, raw_text, matches[0])

    async def _apply_correction(
        self, parsed_msg: ParsedWhatsAppMessage, raw_text: str, original: dict
    ) -> _PipelineOutcome:
        """Run the correction branch against one stored row."""
        message_id = _cell(original, "message_id")
        if not message_id:
            logger.warning("The cited row has no message_id; correction aborted.")
            return _PipelineOutcome(reply=QUOTED_NOT_FOUND_MESSAGE)

        try:
            llm_result = await run_blocking(
                self.groq_client.correct_expense_entities,
                text=raw_text,
                original=self._as_contract_transaction(original),
                reference_date=date.today(),
                timeout=settings.GROQ_TIMEOUT_SECONDS,
                label="expense correction",
            )
        except ServiceTimeoutError:
            logger.error(f"Correction of {message_id} timed out.")
            return _PipelineOutcome(reply=CORRECTION_LLM_ERROR_MESSAGE)
        except Exception as e:
            logger.error(f"LLM correction error: {e}")
            return _PipelineOutcome(reply=CORRECTION_LLM_ERROR_MESSAGE)

        if llm_result is None:
            logger.error(f"LLM returned no correction for message {message_id}.")
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
            updated = await run_blocking(
                self.repository.update_expense,
                record,
                timeout=settings.STORAGE_TIMEOUT_SECONDS,
                label="update expense",
            )
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
            stored_message_ids=[message_id],
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
        try:
            amount = round(float(_cell(stored, "monto").replace(",", ".")), 2)
        except ValueError:
            amount = 0.0

        return {
            "transaction_date": _iso_date(_cell(stored, "fecha_gasto")),
            "transaction_time": None,
            "amount": amount,
            "currency": _cell(stored, "moneda") or "ARS",
            "category": _cell(stored, "categoria") or "Otros",
            "subcategory": _cell(stored, "subcategoria") or "Otros",
            "merchant_name": None,
            "payment_method": _cell(stored, "medio_de_pago") or "No Especificado",
            "description": _cell(stored, "descripcion") or "Gasto registrado",
        }

    @staticmethod
    def _correction_confirmation(original: dict, record: ExpenseRecord) -> str:
        """WhatsApp summary of the updated expense, marking the fields that actually changed."""

        def stored(name: str) -> str:
            return _cell(original, name)

        def change(old: str, new: str) -> str:
            return f" (antes {old})" if old and old != new else ""

        try:
            old_amount: Optional[float] = round(float(stored("monto").replace(",", ".")), 2)
        except ValueError:
            old_amount = None

        old_currency = stored("moneda") or record.currency
        new_date = record.transaction_date.isoformat()
        new_category = f"{record.category} ({record.subcategory})"
        old_category = f"{stored('categoria')} ({stored('subcategoria')})"
        amount = f"{record.amount:,.2f} {record.currency}"
        if old_amount is not None and old_amount != round(record.amount, 2):
            amount += f" (antes {old_amount:,.2f} {old_currency})"

        return (
            "✏️ *Registro modificado*\n\n"
            f"💰 *Monto:* {amount}\n"
            f"📅 *Fecha:* {new_date}{change(_iso_date(stored('fecha_gasto')), new_date)}\n"
            f"📂 *Categoría:* {new_category}{change(old_category, new_category)}\n"
            f"📝 *Detalle:* {record.description}{change(stored('descripcion'), record.description)}\n"
            f"💳 *Método:* {record.payment_method}{change(stored('medio_de_pago'), record.payment_method)}"
        )
