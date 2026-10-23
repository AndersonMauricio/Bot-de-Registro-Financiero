"""PostgreSQL repository implementation for Target Analytics Architecture."""

from typing import Optional
from sqlalchemy import Column, Date, Float, MetaData, String, Table, Text, Time, create_engine, select
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
        """Check if message_id exists in raw_whatsapp.expenses."""
        try:
            with self.engine.connect() as conn:
                query = select(expenses_table.c.transaction_id).where(
                    expenses_table.c.message_id == message_id
                )
                result = conn.execute(query).scalar_one_or_none()
                return result is not None
        except Exception as e:
            logger.error(f"Error querying message_id in PostgreSQL: {e}")
            return False

    def save_expense(self, record: ExpenseRecord) -> str:
        """Insert expense record into PostgreSQL."""
        try:
            with self.engine.begin() as conn:
                stmt = expenses_table.insert().values(
                    transaction_id=record.transaction_id,
                    message_id=record.message_id,
                    user_phone_hash=record.user_phone_hash,
                    input_type=record.input_type.value,
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
                conn.execute(stmt)
            logger.info(f"Expense {record.transaction_id} saved in PostgreSQL.")
            return str(record.transaction_id)
        except Exception as e:
            logger.error(f"Failed to persist expense in PostgreSQL: {e}")
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
                    {
                        "fecha_gasto": row.transaction_date.isoformat(),
                        "monto": row.amount,
                        "moneda": row.currency,
                        "categoria": row.category,
                        "subcategoria": row.subcategory or "",
                        "medio_de_pago": row.payment_method,
                        "descripcion": row.description,
                    }
                    for row in conn.execute(query)
                ]
        except Exception as e:
            logger.error(f"Failed to read expenses from PostgreSQL: {e}")
            raise StorageError(f"PostgreSQL read failed: {e}") from e
