"""Abstract storage interface defining the Repository pattern for expenses."""

from abc import ABC, abstractmethod
from src.domain.models import ExpenseRecord


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
