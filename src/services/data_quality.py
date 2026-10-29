"""Data Quality Gate: sanitization, PII hashing, and validation engine."""

import hashlib
import re
from datetime import date
from typing import Optional
from src.core.config import settings
from src.domain.enums import InputTypeEnum
from src.domain.models import (
    DataQualityResult,
    ExpenseRecord,
    ExtractionMetadata,
    ExtractedTransaction,
    LLMExtractionResponse,
)


class DataQualityGate:
    """Enforces data contracts, privacy compliance, and integrity rules."""

    MIN_CONFIDENCE_THRESHOLD: float = 0.65
    VALID_CURRENCY_REGEX = re.compile(r"^[A-Z]{3}$")
    FORMULA_INJECTION_PREFIXES = ("=", "+", "-", "@")

    @classmethod
    def hash_phone_number(cls, phone_number: str) -> str:
        """Hash phone number using SHA-256 to ensure PII compliance."""
        clean_phone = re.sub(r"\D", "", phone_number)
        return hashlib.sha256(clean_phone.encode("utf-8")).hexdigest()

    @classmethod
    def sanitize_string_for_sheets(cls, value: Optional[str]) -> str:
        """Sanitize text to prevent CSV/Formula injection in spreadsheets."""
        if not value:
            return ""
        trimmed = value.strip()
        if trimmed.startswith(cls.FORMULA_INJECTION_PREFIXES):
            return f"'{trimmed}"
        return trimmed

    @classmethod
    def validate_and_sanitize(
        cls,
        llm_response: LLMExtractionResponse,
        message_id: str,
        user_phone: str,
        input_type: InputTypeEnum,
    ) -> DataQualityResult:
        """Validate a single-expense extraction (correction branch). Returns the first result of the batch."""
        return cls.validate_transactions(llm_response, message_id, user_phone, input_type)[0]

    @classmethod
    def validate_transactions(
        cls,
        llm_response: LLMExtractionResponse,
        message_id: str,
        user_phone: str,
        input_type: InputTypeEnum,
    ) -> list[DataQualityResult]:
        """Validate and sanitize every expense of the message, one result per transaction in order.

        Each record gets its own id: a single expense keeps the message id (idempotency as before), while a
        batch uses `<message_id>-0`, `<message_id>-1`, ... so every row stays individually addressable.
        One invalid item does not invalidate the others: the caller decides what to do with each result.
        """
        meta = llm_response.metadata
        transactions = llm_response.transactions

        if not transactions:
            return [
                DataQualityResult(
                    is_valid=False,
                    confidence_score=meta.confidence_score,
                    errors=["No se detectó ningún gasto en el mensaje."],
                )
            ]

        if len(transactions) > settings.MAX_TRANSACTIONS_PER_MESSAGE:
            return [
                DataQualityResult(
                    is_valid=False,
                    confidence_score=meta.confidence_score,
                    errors=[
                        f"El mensaje describe {len(transactions)} gastos y el máximo por mensaje es "
                        f"{settings.MAX_TRANSACTIONS_PER_MESSAGE}. Enviálos en mensajes separados."
                    ],
                )
            ]

        ids = (
            [message_id]
            if len(transactions) == 1
            else [f"{message_id}-{index}" for index in range(len(transactions))]
        )
        return [
            cls._validate_one(meta, tx, record_id, user_phone, input_type)
            for tx, record_id in zip(transactions, ids)
        ]

    @classmethod
    def _validate_one(
        cls,
        meta: ExtractionMetadata,
        tx: ExtractedTransaction,
        message_id: str,
        user_phone: str,
        input_type: InputTypeEnum,
    ) -> DataQualityResult:
        """Business rules for one transaction, plus the sanitized record ready to persist."""
        errors: list[str] = []
        warnings: list[str] = []

        # 1. Confidence Score Check
        if meta.confidence_score < cls.MIN_CONFIDENCE_THRESHOLD:
            errors.append(
                f"La confianza de la extracción ({meta.confidence_score:.2f}) es menor al mínimo aceptable "
                f"({cls.MIN_CONFIDENCE_THRESHOLD:.2f})."
            )

        # 2. Amount Validation
        if tx.amount <= 0:
            errors.append(f"El monto ({tx.amount}) debe ser un número mayor a 0.")

        # 3. Currency Validation
        currency_clean = tx.currency.upper().strip()
        if not cls.VALID_CURRENCY_REGEX.match(currency_clean):
            errors.append(f"La moneda '{tx.currency}' debe ser un código ISO 4217 de 3 letras mayúsculas.")

        # 4. Detail Validation
        if not tx.description.strip():
            errors.append("Falta el detalle (concepto) del gasto.")

        # 5. Future Date Check (Warning, not blocking)
        today = date.today()
        if tx.transaction_date > today:
            warnings.append(
                f"La fecha del gasto ({tx.transaction_date.isoformat()}) es futura; hoy es {today.isoformat()}."
            )

        # If errors encountered, return invalid result
        if errors:
            return DataQualityResult(
                is_valid=False,
                confidence_score=meta.confidence_score,
                errors=errors,
                warnings=warnings,
                sanitized_record=None,
            )

        # Build sanitized persistent entity
        sanitized_record = ExpenseRecord(
            message_id=message_id,
            user_phone_hash=cls.hash_phone_number(user_phone),
            input_type=input_type,
            transaction_date=tx.transaction_date,
            transaction_time=tx.transaction_time,
            amount=round(tx.amount, 2),
            currency=currency_clean,
            category=tx.category.value,
            subcategory=cls.sanitize_string_for_sheets(tx.subcategory),
            merchant_name=cls.sanitize_string_for_sheets(tx.merchant_name) if tx.merchant_name else None,
            payment_method=tx.payment_method.value,
            description=cls.sanitize_string_for_sheets(tx.description),
            confidence_score=meta.confidence_score,
            raw_text=cls.sanitize_string_for_sheets(meta.raw_text),
        )

        return DataQualityResult(
            is_valid=True,
            confidence_score=meta.confidence_score,
            errors=[],
            warnings=warnings,
            sanitized_record=sanitized_record,
        )
