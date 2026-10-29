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

    @abstractmethod
    def get_expenses_by_user(self, user_phone_hash: str) -> list[dict]:
        """Return only the expenses of one user (strict match on user_phone_hash), as plain dicts.

        Keys: fecha_gasto, monto, moneda, categoria, subcategoria, medio_de_pago, descripcion.
        """
        pass

    @abstractmethod
    def find_expense_by_message_id(self, message_id: str, user_phone_hash: str) -> Optional[dict]:
        """Return one of this user's own expenses by its incoming message id or the bot reply id.

        Keys: the EXPENSE_SHEET_HEADERS names the storage holds (always message_id, fecha_gasto, monto,
        moneda, categoria, subcategoria, medio_de_pago and descripcion). None when the id is unknown or
        belongs to another user.
        """
        pass

    @abstractmethod
    def update_expense(self, record: ExpenseRecord) -> bool:
        """Overwrite the data columns of the row whose message_id is `record.message_id` (same owner only).

        Never touches the identity columns (message_id, user_phone_hash) nor the ingestion timestamp.
        Returns False when the row no longer exists.
        """
        pass

    @abstractmethod
    def set_reply_message_id(self, message_id: str, reply_message_id: str, user_phone_hash: str) -> None:
        """Remember the id of the bot confirmation sent for that expense, so quoting it later resolves the row."""
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

    @abstractmethod
    def invalidate(self, phone: str) -> None:
        """The record of this phone changed outside the bot: forget what is cached for it."""
        pass

    @abstractmethod
    def invalidate_all(self) -> None:
        """Forget everything cached about users."""
        pass


class ReportPublisherInterface(ABC):
    """Abstract interface that delivers a user's expense report as a private, read-only document."""

    @abstractmethod
    def publish(self, title: str, rows: list[dict], share_with_email: str) -> str:
        """Create the report with `rows`, grant read-only access to `share_with_email` and return its URL."""
        pass
