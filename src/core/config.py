"""Core configuration module powered by Pydantic Settings."""

import re
from typing import Optional
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment variable loading and validation."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # General App Config
    APP_NAME: str = Field(default="WhatsApp Expense Tracker")
    ENVIRONMENT: str = Field(default="development")
    DEBUG: bool = Field(default=False)
    LOG_LEVEL: str = Field(default="INFO")
    PORT: int = Field(default=8000)
    TIMEZONE: str = Field(default="America/Argentina/Buenos_Aires")

    # Groq & LLM Config
    GROQ_API_KEY: SecretStr = Field(default=SecretStr(""))
    GROQ_LLM_MODEL: str = Field(default="openai/gpt-oss-20b")
    GROQ_WHISPER_MODEL: str = Field(default="whisper-large-v3")

    # Evolution API (WhatsApp gateway)
    EVOLUTION_API_URL: str = Field(default="http://localhost:8080")
    EVOLUTION_API_KEY: SecretStr = Field(default=SecretStr(""))
    EVOLUTION_INSTANCE: str = Field(default="")
    # Shared secret Evolution sends in the `x-webhook-secret` header (set via scripts/setup_evolution_webhook.py)
    EVOLUTION_WEBHOOK_SECRET: SecretStr = Field(default=SecretStr(""))

    # Google Sheets Integration (MVP)
    # Allows specifying path or base64 encoded service account json
    GOOGLE_APPLICATION_CREDENTIALS: Optional[str] = Field(default=None)
    GCP_SERVICE_ACCOUNT_B64: Optional[SecretStr] = Field(default=None)
    SPREADSHEET_ID: str = Field(
        default="",
        validation_alias=AliasChoices("SPREADSHEET_ID", "GOOGLE_SHEETS_SHEET_ID"),
    )
    WORKSHEET_NAME: str = Field(
        default="raw_expenses",
        validation_alias=AliasChoices("WORKSHEET_NAME", "GOOGLE_SHEETS_TAB_NAME"),
    )

    @field_validator("SPREADSHEET_ID")
    @classmethod
    def clean_spreadsheet_id(cls, v: str) -> str:
        if not v:
            return ""
        clean = v.strip()
        if "/d/" in clean:
            match = re.search(r"/d/([a-zA-Z0-9-_]+)", clean)
            if match:
                return match.group(1)
        if "/" in clean:
            return clean.split("/")[0].strip()
        return clean

    @field_validator("WORKSHEET_NAME")
    @classmethod
    def clean_worksheet_name(cls, v: str) -> str:
        return v.strip() if v else "raw_expenses"

    # Users access control (tab in the same spreadsheet): 1 = approved, 2 = rejected, empty = unknown/pending
    USERS_WORKSHEET_NAME: str = Field(default="usuarios")
    # How long the users table is served from memory before re-reading the sheet (Sheets read quota)
    USER_CACHE_TTL_SECONDS: int = Field(default=60, ge=1)

    # Shared secret the Google Apps Script trigger sends in `x-webhook-secret` when the `estado` column is edited
    SHEETS_WEBHOOK_SECRET: SecretStr = Field(default=SecretStr(""))

    # Reports on demand: optional Drive folder (shared with the administrators) where report spreadsheets are
    # created, and the minimum seconds between two reports for the same user
    REPORTS_FOLDER_ID: str = Field(default="")
    REPORT_COOLDOWN_SECONDS: int = Field(default=300, ge=0)

    @field_validator("USERS_WORKSHEET_NAME")
    @classmethod
    def clean_users_worksheet_name(cls, v: str) -> str:
        return v.strip() if v else "usuarios"

    # Storage Backend Selector ("sheets" | "postgres")
    STORAGE_BACKEND: str = Field(default="sheets")

    # PostgreSQL Config (Phase 2 Target)
    POSTGRES_HOST: str = Field(default="localhost")
    POSTGRES_PORT: int = Field(default=5432)
    POSTGRES_DB: str = Field(default="analytics_db")
    POSTGRES_USER: str = Field(default="data_engineer")
    POSTGRES_PASSWORD: SecretStr = Field(default=SecretStr("secret_password"))

    @property
    def postgres_connection_string(self) -> str:
        """Construct PostgreSQL SQLAlchemy connection string."""
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:"
            f"{self.POSTGRES_PASSWORD.get_secret_value()}@"
            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


settings = Settings()
