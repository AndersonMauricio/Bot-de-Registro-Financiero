"""Contract checks for the SQL side of the edit mode (no database connection is opened)."""

from sqlalchemy import String
from src.infrastructure.storage.base import ExpenseRepositoryInterface
from src.infrastructure.storage.postgres_repo import expenses_table


def test_the_expenses_table_can_remember_the_bot_confirmation():
    column = expenses_table.c.reply_message_id

    assert isinstance(column.type, String)
    assert column.type.length == 128
    assert column.nullable is True  # rows registered before this feature have no confirmation id


def test_the_edit_operations_are_part_of_the_repository_contract():
    assert {
        "find_expense_by_message_id",
        "update_expense",
        "set_reply_message_id",
    } <= ExpenseRepositoryInterface.__abstractmethods__
