"""PostgreSQL repository implementation for Target Analytics Architecture."""

from typing import Optional
from sqlalchemy import Column, Date, Float, MetaData, String, Table, Text, Time, create_engine, or_, select, update
from sqlalchemy.dialects.postgresql import UUID
from src.core.config import settings
from src.core.exceptions import StorageError
from src.core.logging import logger
from src.domain.models import ExpenseRecord
from src.infrastructure.storage.base import ExpenseRepositoryInterface

metadata = MetaData(schema="raw_whatsapp")

expenses_table = Table(
    "expenses",
    metadata,
    Column("transaction_id", UUID(as_uuid=True), primary_key=True),
    Column("message_id", String(128), unique=True, nullable=False),
    Column("user_phone_hash", String(64), nullable=False),
    Column("input_type", String(20), nullable=False),
    Column("transaction_date", Date, nullable=False),
    Column("transaction_time", Time, nullable=True),
    Column("amount", Float, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("category", String(50), nullable=False),
    Column("subcategory", String(50), nullable=True),
    Column("merchant_name", String(100), nullable=True),
    Column("payment_method", String(30), nullable=False),
    Column("description", Text, nullable=False),
    Column("confidence_score", Float, nullable=False),
    Column("raw_text", Text, nullable=False),
    # ID of the bot's confirmation message: lets the user correct this expense by replying to that reply
    Column("reply_message_id", String(128), nullable=True),
)


class PostgresExpenseRepository(ExpenseRepositoryInterface):
    """Persists expenses directly into PostgreSQL database."""

    def __init__(self):
        try:
            self.engine = create_engine(
                settings.postgres_connection_string,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
            )
        except Exception as e:
            logger.error(f"Error creating PostgreSQL engine: {e}")
            raise StorageError(f"PostgreSQL connection initialization failed: {e}") from e

    def is_message_processed(self, message_id: str) -> bool:
        """Check if message_id exists in raw_whatsapp.expenses (or any of its batch rows `<id>-<n>`)."""
        escaped = message_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        try:
            with self.engine.connect() as conn:
                query = select(expenses_table.c.transaction_id).where(
                    or_(
                        expenses_table.c.message_id == message_id,
                        expenses_table.c.message_id.like(f"{escaped}-%", escape="\\"),
                    )
                )
                return conn.execute(query).first() is not None
        except Exception as e:
            logger.error(f"Error querying message_id in PostgreSQL: {e}")
            return False

    @staticmethod
    def _values(record: ExpenseRecord) -> dict:
        """Row values for one expense."""
        return {
            "transaction_id": record.transaction_id,
            "message_id": record.message_id,
            "user_phone_hash": record.user_phone_hash,
            "input_type": record.input_type.value,
            "transaction_date": record.transaction_date,
            "transaction_time": record.transaction_time,
            "amount": record.amount,
            "currency": record.currency,
            "category": record.category,
            "subcategory": record.subcategory,
            "merchant_name": record.merchant_name,
            "payment_method": record.payment_method,
            "description": record.description,
            "confidence_score": record.confidence_score,
            "raw_text": record.raw_text,
        }

    def save_expenses(self, records: list[ExpenseRecord]) -> list[str]:
        """Insert every record in one transaction: either all rows land or none do."""
        if not records:
            return []
        try:
            with self.engine.begin() as conn:
                conn.execute(expenses_table.insert(), [self._values(record) for record in records])
            logger.info(f"{len(records)} expense(s) saved in PostgreSQL.")
            return [str(record.transaction_id) for record in records]
        except Exception as e:
            logger.error(f"Failed to persist expenses in PostgreSQL: {e}")
            raise StorageError(f"PostgreSQL insert failed: {e}") from e

    def get_expenses_by_user(self, user_phone_hash: str) -> list[dict]:
        """Rows of one user (strict match on user_phone_hash), oldest first."""
        if not user_phone_hash:
            return []
        try:
            with self.engine.connect() as conn:
                query = (
                    select(expenses_table)
                    .where(expenses_table.c.user_phone_hash == user_phone_hash)
                    .order_by(expenses_table.c.transaction_date, expenses_table.c.transaction_time)
                )
                return [
                    self._row_to_dict(row)
                    for row in conn.execute(query)
                ]
        except Exception as e:
            logger.error(f"Failed to read expenses from PostgreSQL: {e}")
            raise StorageError(f"PostgreSQL read failed: {e}") from e

    @staticmethod
    def _row_to_dict(row) -> dict:
        """Row of `raw_whatsapp.expenses` keyed by the report/contract names used everywhere else."""
        return {
            "fecha_gasto": row.transaction_date.isoformat(),
            "monto": row.amount,
            "moneda": row.currency,
            "categoria": row.category,
            "subcategoria": row.subcategory or "",
            "medio_de_pago": row.payment_method,
            "descripcion": row.description,
            "message_id": row.message_id,
            "reply_message_id": row.reply_message_id or "",
        }

    def find_expenses_by_message_id(self, message_id: str, user_phone_hash: str) -> list[dict]:
        """All of this user's expenses cited by `message_id` (a batch shares the confirmation id)."""
        if not (message_id and user_phone_hash):
            return []
        try:
            with self.engine.connect() as conn:
                query = (
                    select(expenses_table)
                    .where(
                        or_(
                            expenses_table.c.message_id == message_id,
                            expenses_table.c.reply_message_id == message_id,
                        ),
                        expenses_table.c.user_phone_hash == user_phone_hash,
                    )
                    .order_by(expenses_table.c.message_id)
                )
                return [self._row_to_dict(row) for row in conn.execute(query)]
        except Exception as e:
            logger.error(f"Failed to find an expense by message id in PostgreSQL: {e}")
            raise StorageError(f"PostgreSQL read failed: {e}") from e

    def find_expense_by_message_id(self, message_id: str, user_phone_hash: str) -> Optional[dict]:
        """First of this user's own expenses cited by `message_id`."""
        matches = self.find_expenses_by_message_id(message_id, user_phone_hash)
        return matches[0] if matches else None

    def update_expense(self, record: ExpenseRecord) -> bool:
        """Overwrite the data columns of the row of `record.message_id`, only if it belongs to that user."""
        try:
            with self.engine.begin() as conn:
                stmt = (
                    update(expenses_table)
                    .where(
                        expenses_table.c.message_id == record.message_id,
                        expenses_table.c.user_phone_hash == record.user_phone_hash,
                    )
                    .values(
                        transaction_date=record.transaction_date,
                        transaction_time=record.transaction_time,
                        amount=record.amount,
                        currency=record.currency,
                        category=record.category,
                        subcategory=record.subcategory,
                        merchant_name=record.merchant_name,
                        payment_method=record.payment_method,
                        description=record.description,
                        confidence_score=record.confidence_score,
                        raw_text=record.raw_text,
                    )
                )
                updated = conn.execute(stmt).rowcount > 0
            if not updated:
                logger.warning(f"Expense {record.message_id} is no longer in PostgreSQL; nothing was updated.")
            else:
                logger.info(f"Expense {record.message_id} updated in PostgreSQL.")
            return updated
        except Exception as e:
            logger.error(f"Failed to update an expense in PostgreSQL: {e}")
            raise StorageError(f"PostgreSQL update failed: {e}") from e

    def set_reply_message_id(self, message_id: str, reply_message_id: str, user_phone_hash: str) -> None:
        """Remember the bot confirmation id of an expense. Best effort: the user was already answered."""
        if not (message_id and reply_message_id):
            return
        try:
            with self.engine.begin() as conn:
                stmt = (
                    update(expenses_table)
                    .where(
                        expenses_table.c.message_id == message_id,
                        expenses_table.c.user_phone_hash == user_phone_hash,
                    )
                    .values(reply_message_id=reply_message_id)
                )
                conn.execute(stmt)
        except Exception as e:
            # Losing the link only degrades later corrections: never fail the answer that is already sent
            logger.warning(f"Could not store the reply id of expense {message_id}: {e}")
