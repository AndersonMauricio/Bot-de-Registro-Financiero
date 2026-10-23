"""Dependency injection containers and factories for FastAPI."""

from functools import lru_cache
from fastapi import Depends
from src.core.config import Settings, settings
from src.infrastructure.audio.whisper_client import WhisperClient
from src.infrastructure.llm.groq_client import GroqLLMClient
from src.infrastructure.storage.base import ExpenseRepositoryInterface, UserRepositoryInterface
from src.infrastructure.storage.postgres_repo import PostgresExpenseRepository
from src.infrastructure.storage.sheets_repo import GoogleSheetsExpenseRepository
from src.infrastructure.storage.sheets_users_repo import GoogleSheetsUserRepository
from src.infrastructure.whatsapp.client import EvolutionAPIClient
from src.services.expense_service import ExpenseProcessingService
from src.services.message_router import MessageRouter
from src.services.onboarding_service import OnboardingService


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
def get_whatsapp_client() -> EvolutionAPIClient:
    """Return singleton instance of the Evolution API client."""
    return EvolutionAPIClient()


@lru_cache()
def get_expense_repository() -> ExpenseRepositoryInterface:
    """Dynamically return repository implementation based on configuration."""
    backend = settings.STORAGE_BACKEND.lower()
    if backend == "postgres":
        return PostgresExpenseRepository()
    return GoogleSheetsExpenseRepository()


@lru_cache()
def get_user_repository() -> UserRepositoryInterface:
    """Users always live in Google Sheets (cached in memory), whichever backend stores the expenses."""
    return GoogleSheetsUserRepository()


def get_expense_service() -> ExpenseProcessingService:
    """Construct ExpenseProcessingService with injected dependencies."""
    return ExpenseProcessingService(
        whisper_client=get_whisper_client(),
        groq_client=get_groq_client(),
        repository=get_expense_repository(),
        whatsapp_client=get_whatsapp_client(),
    )


@lru_cache()
def get_message_router() -> MessageRouter:
    """Return the singleton router (it keeps the duplicate-delivery cache and per-sender locks)."""
    whatsapp_client = get_whatsapp_client()
    user_repository = get_user_repository()
    return MessageRouter(
        user_repository=user_repository,
        expense_service=get_expense_service(),
        onboarding_service=OnboardingService(
            groq_client=get_groq_client(),
            user_repository=user_repository,
            whatsapp_client=whatsapp_client,
        ),
        whatsapp_client=whatsapp_client,
    )
