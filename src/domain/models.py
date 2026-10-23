"""Domain entities and data contracts using Pydantic v2."""

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field, field_validator
from src.core.config import settings
from src.domain.enums import CategoryEnum, InputTypeEnum, PaymentMethodEnum


class ExtractionMetadata(BaseModel):
    """Metadata generated during LLM extraction process."""
    confidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence level of extraction between 0.0 and 1.0",
    )
    raw_text: str = Field(..., description="Raw text, transcription or OCR transcript")
    extraction_notes: Optional[str] = Field(
        default=None,
        description="Ambiguity notes or observations from LLM",
    )


class ExtractedTransaction(BaseModel):
    """Financial transaction entity returned by LLM extraction."""
    transaction_date: date = Field(..., description="Purchase date in ISO 8601 (YYYY-MM-DD)")
    transaction_time: Optional[time] = Field(
        default=None,
        description="Transaction time (HH:mm:ss) or None",
    )
    amount: float = Field(..., gt=0, description="Total amount with up to 2 decimal places")
    currency: str = Field(..., min_length=3, max_length=3, description="ISO 4217 currency code")
    category: CategoryEnum = Field(default=CategoryEnum.OTROS, description="Macro category")
    subcategory: str = Field(default="Otros", description="Granular subcategory")
    merchant_name: Optional[str] = Field(default=None, description="Merchant or vendor name")
    payment_method: PaymentMethodEnum = Field(
        default=PaymentMethodEnum.UNKNOWN,
        description="Normalized payment method",
    )
    description: str = Field(..., min_length=1, description="Concise expense description")

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("amount")
    @classmethod
    def round_amount(cls, v: float) -> float:
        return round(v, 2)


class LLMExtractionResponse(BaseModel):
    """Top-level contract expected from the LLM."""
    metadata: ExtractionMetadata
    transaction: ExtractedTransaction


class ExpenseRecord(BaseModel):
    """Full persistence record ready for Storage (Google Sheets or PostgreSQL)."""
    transaction_id: UUID = Field(default_factory=uuid4)
    message_id: str = Field(..., description="WhatsApp message ID for idempotency")
    user_phone_hash: str = Field(..., min_length=64, max_length=64, description="SHA-256 of phone")
    input_type: InputTypeEnum = Field(..., description="Audio, Text or Image")
    transaction_date: date
    transaction_time: Optional[time] = None
    amount: float
    currency: str
    category: str
    subcategory: str
    merchant_name: Optional[str] = None
    payment_method: str
    description: str
    confidence_score: float
    raw_text: str
    ingested_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_sheets_row(self) -> list:
        """Convert record to ordered list matching Google Sheets schema:
        [timestamp, fecha_gasto, monto, moneda, categoria, descripcion]

        - timestamp   : fecha y hora de ingesta del registro (UTC-3 Argentina)
        - fecha_gasto : fecha real del gasto resuelto por el LLM (ej. 'ayer' → yesterday)
        """
        try:
            local_tz = ZoneInfo(settings.TIMEZONE)
            local_dt = self.ingested_at_utc.astimezone(local_tz)
        except Exception:
            local_tz = timezone(timedelta(hours=-3))
            local_dt = self.ingested_at_utc.astimezone(local_tz)

        timestamp_formatted = local_dt.strftime("%Y-%m-%d %H:%M:%S")
        fecha_gasto = self.transaction_date.isoformat()  # YYYY-MM-DD
        return [
            timestamp_formatted,  # timestamp de ingesta (UTC-3)
            fecha_gasto,           # fecha_gasto (fecha real del gasto)
            self.amount,           # monto
            self.currency,         # moneda
            self.category,         # categoria
            self.description,      # descripcion
        ]


class DataQualityResult(BaseModel):
    """Validation report from the Data Quality Gate."""
    is_valid: bool
    confidence_score: float
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sanitized_record: Optional[ExpenseRecord] = None
