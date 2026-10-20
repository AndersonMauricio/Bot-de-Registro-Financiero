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

    # WhatsApp Cloud API Config
    WHATSAPP_TOKEN: SecretStr = Field(default=SecretStr(""))
    WHATSAPP_PHONE_NUMBER_ID: str = Field(default="")
    WHATSAPP_VERIFY_TOKEN: str = Field(default="secret_verify_token")
    WHATSAPP_API_VERSION: str = Field(default="v20.0")

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
