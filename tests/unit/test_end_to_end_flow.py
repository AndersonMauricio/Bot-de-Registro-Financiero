"""End-to-end flow through the real webhook, router, services, users repository and Evolution client.

Only the edges are simulated: Evolution's HTTP API (MockTransport), the Google worksheet, Groq and Whisper.
"""

import json
import httpx
import pytest
from fastapi.testclient import TestClient
from src.api.v1.dependencies import get_message_router, get_settings
from src.core.config import Settings
from src.domain.models import OnboardingData, OnboardingLLMResponse
from src.infrastructure.storage.sheets_users_repo import HEADERS, GoogleSheetsUserRepository
from src.infrastructure.whatsapp.client import EvolutionAPIClient
from src.main import app
from src.services.expense_service import ExpenseProcessingService
from src.services.message_router import ACCESS_DENIED_MESSAGE, MessageRouter
from src.services.onboarding_service import (
    PENDING_APPROVAL_MESSAGE,
    PENDING_REMINDER_MESSAGE,
    OnboardingService,
)
from tests.fakes import FakeClock, FakeExpenseRepository, FakeGroq, FakeWhisper, FakeWorksheet

SECRET = "e2e-secret"
PHONE = "5491122334455"
URL = "/api/v1/webhooks/evolution"


class Gateway:
    """Fake Evolution API: records the outgoing calls the bot makes."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []  # (endpoint, json body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.split("/")[1:3]
        self.calls.append(("/".join(endpoint), json.loads(request.content)))
        return httpx.Response(201, json={"ok": True})

    def sent_texts(self) -> list[str]:
        return [body["text"] for endpoint, body in self.calls if endpoint == "message/sendText"]

    def presence_calls(self) -> list[dict]:
        return [body for endpoint, body in self.calls if endpoint == "chat/sendPresence"]


@pytest.fixture
def system(sample_valid_llm_response):
    gateway = Gateway()
    clock = FakeClock()
    worksheet = FakeWorksheet([HEADERS])
    groq = FakeGroq(delay=0.05)  # slow enough for the typing presence to go out while it "works"
    groq.expense_result = sample_valid_llm_response
    expenses = FakeExpenseRepository()

    whatsapp = EvolutionAPIClient(
        base_url="http://evolution.test",
        api_key="test-key",
        instance="test-instance",
        presence_chunk_ms=20_000,
        transport=httpx.MockTransport(gateway),
    )
    users = GoogleSheetsUserRepository(worksheet=worksheet, clock=clock)
    router = MessageRouter(
        user_repository=users,
        expense_service=ExpenseProcessingService(FakeWhisper(), groq, expenses, whatsapp),
        onboarding_service=OnboardingService(groq, users, whatsapp),
        whatsapp_client=whatsapp,
    )

    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, EVOLUTION_WEBHOOK_SECRET=SECRET, EVOLUTION_INSTANCE="test-instance"
    )
    app.dependency_overrides[get_message_router] = lambda: router
    client = TestClient(app)
    counter = iter(range(1, 1000))

    def send(text: str):
        from_phone = f"{PHONE}@s.whatsapp.net"
        payload = {
            "event": "messages.upsert",
            "instance": "test-instance",
            "data": {
                "key": {"remoteJid": from_phone, "fromMe": False, "id": f"MSG{next(counter):04d}"},
                "message": {"conversation": text},
                "messageType": "conversation",
            },
        }
        response = client.post(URL, json=payload, headers={"x-webhook-secret": SECRET})
        assert response.status_code == 200

    yield send, gateway, worksheet, clock, groq, expenses
    app.dependency_overrides.clear()


def test_full_lifecycle_unknown_user_onboarding_manual_approval_expense_and_rejection(system):
    send, gateway, worksheet, clock, groq, expenses = system

    # --- State null: a stranger writes. The bot starts the onboarding and registers nothing.
    groq.onboarding_result = OnboardingLLMResponse(reply="¡Hola! ¿Cuál es tu nombre y apellido?")
    send("hola, gasté 500 en uber")
    assert gateway.sent_texts() == ["¡Hola! ¿Cuál es tu nombre y apellido?"]
    assert expenses.saved == [] and groq.expense_calls == []
    assert len(worksheet.rows) == 1  # header only: no row for a number that gave no data yet

    # "Escribiendo..." was sent while Groq worked, and before the reply
    first_presence = next(i for i, (e, _) in enumerate(gateway.calls) if e == "chat/sendPresence")
    first_text = next(i for i, (e, _) in enumerate(gateway.calls) if e == "message/sendText")
    assert first_presence < first_text
    assert gateway.presence_calls()[0] == {"number": PHONE, "presence": "composing", "delay": 20_000}

    # --- Still state null: data arrives piece by piece and is stored in the sheet.
    groq.onboarding_result = OnboardingLLMResponse(
        collected=OnboardingData(full_name="Lucía Fernández"), reply="Gracias Lucía. ¿Cuál es tu email?"
    )
    send("me llamo Lucía Fernández")
    assert worksheet.rows[1][:4] == [PHONE, "", "Lucía Fernández", ""]

    groq.onboarding_result = OnboardingLLMResponse(collected=OnboardingData(email="lucia@example.com"))
    send("lucia@example.com")
    assert worksheet.rows[1][:4] == [PHONE, "", "Lucía Fernández", "lucia@example.com"]
    assert gateway.sent_texts()[-1] == PENDING_APPROVAL_MESSAGE

    # --- Still pending: no more LLM calls, just a reminder. Expenses are still refused.
    llm_calls_before = len(groq.onboarding_calls)
    send("gasté 700 en el súper")
    assert gateway.sent_texts()[-1] == PENDING_REMINDER_MESSAGE
    assert len(groq.onboarding_calls) == llm_calls_before
    assert expenses.saved == []

    # --- State 1: the administrator approves by hand in the sheet; the bot notices once its cache expires.
    worksheet.rows[1][1] = 1
    send("Almorcé sushi por 15.50 USD pagando con débito")
    assert expenses.saved == []  # still served from the in-memory snapshot (Sheets quota protection)
    assert gateway.sent_texts()[-1] == PENDING_REMINDER_MESSAGE

    clock.advance(61)
    send("Almorcé sushi por 15.50 USD pagando con débito")
    assert len(expenses.saved) == 1
    assert "Gasto Registrado con Éxito" in gateway.sent_texts()[-1]

    # --- State 2: the administrator rejects the user.
    worksheet.rows[1][1] = 2
    clock.advance(61)
    send("gasté 100 en café")
    assert gateway.sent_texts()[-1] == ACCESS_DENIED_MESSAGE
    assert len(expenses.saved) == 1


def test_a_stranger_messaging_repeatedly_causes_a_single_sheet_read(system):
    send, gateway, worksheet, _, groq, _ = system
    groq.onboarding_result = OnboardingLLMResponse(reply="¿Cuál es tu nombre?")

    for _ in range(10):
        send("hola")

    assert worksheet.reads == 1
    assert len(gateway.sent_texts()) == 10
