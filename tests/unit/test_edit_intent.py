"""Unit tests for the correction-without-quote detection (deterministic, no LLM)."""

import pytest
from src.domain.enums import InputTypeEnum
from src.services.edit_intent import is_correction_reference

CORRECTIONS = [
    "me equivoqué, eran 155",
    "Me equivoque eran 155 pesos",
    "me confundí, era con tarjeta de débito",
    "en realidad fueron 155",
    "corrijo: eran 155 dólares",
    "quise decir 155",
    "no eran 15, eran 155",
    "me di cuenta que puse mal el monto",
    "el monto estaba mal",
]

NEW_EXPENSES = [
    "gasté 155 USD en café con débito",
    "pagué el súper con tarjeta",
    "compré 2 cafés",
    "",
    "hola",
    "quiero mi resumen de gastos",
]


@pytest.mark.parametrize("text", CORRECTIONS)
def test_a_remark_about_a_stored_expense_is_recognized(text):
    assert is_correction_reference(text) is True


@pytest.mark.parametrize("text", NEW_EXPENSES)
def test_a_new_expense_or_other_message_is_not_mistaken_for_a_correction(text):
    assert is_correction_reference(text) is False


def test_a_new_expense_that_mentions_a_change_is_still_a_new_expense():
    """The spending verb wins: 'gasté 155, en realidad con descuento' registers a new expense."""
    assert is_correction_reference("gasté 155 en café, en realidad tenía descuento") is False


def test_a_long_text_is_never_treated_as_a_correction():
    assert is_correction_reference("me equivoqué " + "y " * 40) is False


def test_the_service_asks_for_the_quote_instead_of_failing(monkeypatch):
    """Reproduces the real case: 'me equivoqué, eran 155' sent without quoting the confirmation."""
    import asyncio
    from src.infrastructure.whatsapp.parser import ParsedWhatsAppMessage
    from src.services.expense_service import ExpenseProcessingService
    from tests.fakes import FakeExpenseRepository, FakeGroq, FakeWhatsApp, FakeWhisper

    async def run():
        groq, repo, whatsapp = FakeGroq(), FakeExpenseRepository(), FakeWhatsApp()
        service = ExpenseProcessingService(FakeWhisper(), groq, repo, whatsapp)
        message = ParsedWhatsAppMessage(
            message_id="msg-2",
            sender_phone="5491100000001",
            timestamp="1729382400",
            input_type=InputTypeEnum.TEXT,
            text_content="me equivoqué, eran 155",
        )
        return await service.process_incoming_message(message), groq, repo, whatsapp

    result, groq, repo, whatsapp = asyncio.run(run())

    assert result is None
    assert groq.expense_calls == []  # no extraction, no duplicate row
    assert groq.correction_calls == []
    assert repo.saved == []
    assert "cites" in whatsapp.texts[0]
    assert "No registré nada" in whatsapp.texts[0]
