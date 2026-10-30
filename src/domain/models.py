"""Domain entities and data contracts using Pydantic v2."""

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field, field_validator, model_validator
from src.core.config import settings
from src.domain.enums import CategoryEnum, InputTypeEnum, PaymentMethodEnum, UserStatusEnum

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Columns of the `raw_expenses` worksheet, in order (Google Sheets MVP schema aligned with the future PostgreSQL design)
EXPENSE_SHEET_HEADERS = [
    "timestamp",
    "message_id",
    "user_phone_hash",
    "fecha_gasto",
    "monto",
    "moneda",
    "categoria",
    "subcategoria",
    "medio_de_pago",
    "descripcion",
    # ID of the bot's confirmation message: lets a user correct the expense by replying to that reply
    "reply_message_id",
]

# Without `reply_message_id` older worksheets keep working (only quoting the user's own message resolves)
OPTIONAL_EXPENSE_SHEET_HEADERS = ("reply_message_id",)
REQUIRED_EXPENSE_SHEET_HEADERS = [
    header for header in EXPENSE_SHEET_HEADERS if header not in OPTIONAL_EXPENSE_SHEET_HEADERS
]

# Columns an edit may overwrite (identity and ingestion metadata are never rewritten)
EXPENSE_DATA_COLUMNS = (
    "fecha_gasto",
    "monto",
    "moneda",
    "categoria",
    "subcategoria",
    "medio_de_pago",
    "descripcion",
)


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
    """Financial transaction entity returned by LLM extraction.

    The hard business rules (positive amount, ISO currency, non-empty detail) live in the Data Quality
    Gate, not here: with several expenses per message, one bad item must not discard the whole batch.
    That includes a missing amount: it reaches the gate, which answers it with a friendly message, instead of
    failing the parse (and every model retry) with a server error.
    """
    transaction_date: date = Field(..., description="Purchase date in ISO 8601 (YYYY-MM-DD)")
    transaction_time: Optional[time] = Field(
        default=None,
        description="Transaction time (HH:mm:ss) or None",
    )
    amount: Optional[float] = Field(
        default=None, description="Total amount with up to 2 decimal places (required, validated by the gate)"
    )
    currency: str = Field(..., description="ISO 4217 currency code (validated by the gate)")
    category: CategoryEnum = Field(default=CategoryEnum.OTROS, description="Macro category")
    subcategory: str = Field(default="Otros", description="Granular subcategory")
    merchant_name: Optional[str] = Field(default=None, description="Merchant or vendor name")
    payment_method: PaymentMethodEnum = Field(
        ...,
        description="Normalized payment method in Spanish (mandatory; 'No Especificado' when not mentioned)",
    )
    description: str = Field(default="", description="Concise expense description (validated by the gate)")

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("amount")
    @classmethod
    def round_amount(cls, v: Optional[float]) -> Optional[float]:
        return None if v is None else round(v, 2)


class LLMExtractionResponse(BaseModel):
    """Top-level contract expected from the LLM.

    One message may describe several expenses, so the contract carries a list. The legacy single
    `transaction` key is still accepted (the correction branch and older prompts use it) and the two
    views are kept in sync, so callers can keep reading `response.transaction` for a single expense.
    An empty list is valid: it is how the model says the message is not an expense at all ("Hola", a general
    question), and the Data Quality Gate turns it into an explanatory reply.
    """
    metadata: ExtractionMetadata
    transactions: list[ExtractedTransaction] = Field(
        default_factory=list,
        description="One entry per expense described in the message (empty when the message is not an expense)",
    )
    transaction: Optional[ExtractedTransaction] = Field(
        default=None,
        description="Deprecated single-transaction view; mirrored from `transactions` when there is only one",
    )

    @model_validator(mode="after")
    def sync_single_and_multi_views(self) -> "LLMExtractionResponse":
        if not self.transactions and self.transaction is not None:
            self.transactions = [self.transaction]
        if self.transaction is None and len(self.transactions) == 1:
            self.transaction = self.transactions[0]
        return self

    @property
    def is_batch(self) -> bool:
        """True when the message described more than one expense."""
        return len(self.transactions) > 1


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

    def to_sheets_dict(self) -> dict:
        """Values keyed by the `raw_expenses` column names (see EXPENSE_SHEET_HEADERS).

        - timestamp       : fecha y hora de ingesta del registro, en la zona horaria configurada
        - message_id      : ID del mensaje de Evolution API (llave de idempotencia)
        - user_phone_hash : SHA-256 del número del remitente (el número nunca se escribe en claro)
        - fecha_gasto     : fecha real del gasto resuelta por el LLM (ej. 'ayer' → yesterday)
        """
        try:
            local_tz = ZoneInfo(settings.TIMEZONE)
            local_dt = self.ingested_at_utc.astimezone(local_tz)
        except Exception:
            local_tz = timezone(timedelta(hours=-3))
            local_dt = self.ingested_at_utc.astimezone(local_tz)

        return {
            "timestamp": local_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "message_id": self.message_id,
            "user_phone_hash": self.user_phone_hash,
            "fecha_gasto": self.transaction_date.isoformat(),  # YYYY-MM-DD
            "monto": self.amount,
            "moneda": self.currency,
            "categoria": self.category,
            "subcategoria": self.subcategory,
            "medio_de_pago": self.payment_method,
            "descripcion": self.description,
        }

    def to_sheets_row(self) -> list:
        """Values as an ordered list matching EXPENSE_SHEET_HEADERS (optional columns stay empty)."""
        values = self.to_sheets_dict()
        return [values.get(header, "") for header in EXPENSE_SHEET_HEADERS]


class DataQualityResult(BaseModel):
    """Validation report from the Data Quality Gate."""
    is_valid: bool
    confidence_score: float
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sanitized_record: Optional[ExpenseRecord] = None
    # No expense was found in the message (very low confidence, no transactions or no amount): the user gets an
    # explanation of what the bot does instead of a request to clarify
    no_expense_detected: bool = False


class OnboardingData(BaseModel):
    """Registration fields collected from an unknown user before an administrator reviews the account."""
    full_name: Optional[str] = Field(default=None, description="Nombre y apellido")
    email: Optional[str] = Field(default=None, description="Correo electrónico")

    # Invalid values are dropped (not raised) so the bot simply asks for the field again.
    @field_validator("full_name", mode="before")
    @classmethod
    def clean_full_name(cls, v: Any) -> Optional[str]:
        if not isinstance(v, str):
            return None
        name = " ".join(v.split())
        if len(name) < 2 or not any(c.isalpha() for c in name):
            return None
        return name[:120]

    @field_validator("email", mode="before")
    @classmethod
    def clean_email(cls, v: Any) -> Optional[str]:
        if not isinstance(v, str):
            return None
        email = v.strip().lower()
        return email if len(email) <= 254 and EMAIL_REGEX.match(email) else None

    def missing_fields(self) -> list[str]:
        """Names of the required fields that are still empty."""
        return [name for name in ("full_name", "email") if not getattr(self, name)]

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields()

    def merged_with(self, newer: "OnboardingData") -> "OnboardingData":
        """Combine with newly collected data; non-empty new values win (the user may correct a previous answer)."""
        return OnboardingData(
            full_name=newer.full_name or self.full_name,
            email=newer.email or self.email,
        )


class OnboardingLLMResponse(BaseModel):
    """Top-level contract expected from the onboarding LLM branch: extraction only, the reply is fixed copy.

    A stray `reply` key from the model is ignored (pydantic drops unknown keys), so it never reaches the user.
    """
    collected: OnboardingData = Field(default_factory=OnboardingData)

    @field_validator("collected", mode="before")
    @classmethod
    def collected_defaults_to_empty(cls, v: Any) -> Any:
        return {} if v is None else v


class UserRecord(BaseModel):
    """Entry of the users sheet that drives access control and onboarding."""
    phone: str = Field(..., description="Digits-only WhatsApp number")
    status: Optional[UserStatusEnum] = Field(
        default=None,
        description="1 = approved, 2 = rejected, None = unknown / pending approval",
    )
    full_name: Optional[str] = None
    email: Optional[str] = None

    def onboarding_data(self) -> OnboardingData:
        return OnboardingData(full_name=self.full_name, email=self.email)
