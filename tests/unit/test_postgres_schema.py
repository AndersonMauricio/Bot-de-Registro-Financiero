"""Contract checks for the SQL side of the edit mode (no database connection is opened)."""

import pytest
from sqlalchemy import String
from src.core.exceptions import StorageError
from src.infrastructure.storage import postgres_repo
from src.infrastructure.storage.base import ExpenseRepositoryInterface
from src.infrastructure.storage.postgres_repo import expenses_table


def test_the_expenses_table_can_remember_the_bot_confirmation():
    column = expenses_table.c.reply_message_id

    assert isinstance(column.type, String)
    assert column.type.length == 128
    assert column.nullable is True  # rows registered before this feature have no confirmation id


def test_the_edit_operations_are_part_of_the_repository_contract():
    assert {
        "save_expenses",
        "find_expenses_by_message_id",
        "update_expense",
        "set_reply_message_id",
    } <= ExpenseRepositoryInterface.__abstractmethods__


class FakeConnection:
    """Captures the DDL issued by ensure_schema without opening a connection."""

    def __init__(self, statements: list[str]):
        self.statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, statement):
        self.statements.append(getattr(statement, "text", str(statement)))


class FakeEngine:
    def __init__(self):
        self.statements: list[str] = []

    def begin(self):
        return FakeConnection(self.statements)


def test_ensure_schema_creates_schema_and_table_only_once(monkeypatch):
    created = []
    monkeypatch.setattr(postgres_repo.metadata, "create_all", lambda engine: created.append(engine))
    repository = postgres_repo.PostgresExpenseRepository()
    monkeypatch.setattr(repository, "engine", FakeEngine())

    repository.ensure_schema()
    repository.ensure_schema()

    assert repository.engine.statements == ["CREATE SCHEMA IF NOT EXISTS raw_whatsapp"]
    assert len(created) == 1  # create_all runs once per process


def test_ensure_schema_failure_is_a_storage_error(monkeypatch):
    def boom(engine):
        raise RuntimeError("no permission")

    monkeypatch.setattr(postgres_repo.metadata, "create_all", boom)
    repository = postgres_repo.PostgresExpenseRepository()
    repository.engine = FakeEngine()

    with pytest.raises(StorageError):
        repository.ensure_schema()
