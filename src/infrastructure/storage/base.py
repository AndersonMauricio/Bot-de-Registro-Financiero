"""Abstract storage interface defining the Repository pattern for expenses."""

from abc import ABC, abstractmethod
from typing import Optional
from src.domain.models import ExpenseRecord, OnboardingData, UserRecord


class ExpenseRepositoryInterface(ABC):
    """Abstract interface for persisting expense records."""

    @abstractmethod
    def save_expense(self, record: ExpenseRecord) -> str:
        """Persist an expense record and return its internal/assigned ID."""
        pass

    @abstractmethod
    def is_message_processed(self, message_id: str) -> bool:
        """Check whether a message_id was already persisted (idempotency check)."""
        pass


class UserRepositoryInterface(ABC):
    """Abstract interface for the users access list (approved / rejected / unknown)."""

    @abstractmethod
    def get_user(self, phone: str) -> Optional[UserRecord]:
        """Return the user for a phone number, or None when it is not registered."""
        pass

    @abstractmethod
    def save_onboarding(self, phone: str, data: OnboardingData) -> UserRecord:
        """Create or update the onboarding data of a user. Never changes the approval status."""
        pass
