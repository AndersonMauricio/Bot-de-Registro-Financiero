"""Dependency injection containers and factories for FastAPI."""

from functools import lru_cache
from fastapi import Depends
from src.core.config import Settings, settings
from src.infrastructure.audio.whisper_client import WhisperClient
from src.infrastructure.llm.groq_client import GroqLLMClient
from src.infrastructure.storage.base import ExpenseRepositoryInterface
from src.infrastructure.storage.postgres_repo import PostgresExpenseRepository
from src.infrastructure.storage.sheets_repo import GoogleSheetsExpenseRepository
from src.infrastructure.whatsapp.client import WhatsAppClient
from src.services.expense_service import ExpenseProcessingService


@lru_cache()
def get_settings() -> Settings:
    """Return cached application settings."""
    return settings


@lru_cache()
def get_whisper_client() -> WhisperClient:
    """Return singleton instance of WhisperClient."""
    return WhisperClient()


@lru_cache()
def get_groq_client() -> GroqLLMClient:
    """Return singleton instance of GroqLLMClient."""
    return GroqLLMClient()


@lru_cache()
def get_whatsapp_client() -> WhatsAppClient:
    """Return singleton instance of WhatsAppClient."""
    return WhatsAppClient()


@lru_cache()
def get_expense_repository() -> ExpenseRepositoryInterface:
    """Dynamically return repository implementation based on configuration."""
    backend = settings.STORAGE_BACKEND.lower()
    if backend == "postgres":
        return PostgresExpenseRepository()
    return GoogleSheetsExpenseRepository()


def get_expense_service() -> ExpenseProcessingService:
    """Construct ExpenseProcessingService with injected dependencies."""
    return ExpenseProcessingService(
        whisper_client=get_whisper_client(),
        groq_client=get_groq_client(),
        repository=get_expense_repository(),
        whatsapp_client=get_whatsapp_client(),
    )
