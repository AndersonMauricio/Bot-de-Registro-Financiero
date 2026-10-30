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
from src.services.report_service import ReportService
from src.infrastructure.cache.ttl_cache import TTLCache
from src.api.v1.dependencies import get_user_repository
from src.services.message_router import ACCESS_DENIED_MESSAGE, MessageRouter
from src.services.onboarding_service import (
    ONBOARDING_MISSING_EMAIL_MESSAGE,
    ONBOARDING_NEW_MESSAGE,
    PENDING_APPROVAL_MESSAGE,
    PENDING_REMINDER_MESSAGE,
    OnboardingService,
)
from tests.fakes import FakeClock, FakeExpenseRepository, FakeGroq, FakeReportPublisher, FakeWhisper, FakeWorksheet

SECRET = "e2e-secret"
SHEETS_SECRET = "e2e-sheets-secret"
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
    publisher = FakeReportPublisher()
    reports = ReportService(expenses, users, publisher, cooldown=TTLCache(300, clock=clock))
    router = MessageRouter(
        user_repository=users,
        expense_service=ExpenseProcessingService(FakeWhisper(), groq, expenses, whatsapp, report_service=reports),
        onboarding_service=OnboardingService(groq, users, whatsapp),
        whatsapp_client=whatsapp,
    )

    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, EVOLUTION_WEBHOOK_SECRET=SECRET, EVOLUTION_INSTANCE="test-instance",
        SHEETS_WEBHOOK_SECRET=SHEETS_SECRET,
    )
    app.dependency_overrides[get_message_router] = lambda: router
    app.dependency_overrides[get_user_repository] = lambda: users
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

    def sheets_event(**payload):
        response = client.post("/api/v1/webhooks/sheets", json=payload, headers={"x-webhook-secret": SHEETS_SECRET})
        assert response.status_code == 200
        return response.json()

    send.sheets_event = sheets_event
    send.publisher = publisher
    yield send, gateway, worksheet, clock, groq, expenses
    app.dependency_overrides.clear()


def test_full_lifecycle_unknown_user_onboarding_manual_approval_expense_and_rejection(system):
    send, gateway, worksheet, clock, groq, expenses = system

    # --- State null: a stranger writes. The bot starts the onboarding and registers nothing.
    groq.onboarding_result = OnboardingLLMResponse()  # the message carries no registration data
    send("hola, gasté 500 en uber")
    assert gateway.sent_texts() == [ONBOARDING_NEW_MESSAGE]
    assert expenses.saved == [] and groq.expense_calls == []
    # the number is registered at first contact (Fecha_Registro set), with no data and no estado
    assert len(worksheet.rows) == 2
    assert worksheet.rows[1][:4] == [PHONE, "", "", ""] and worksheet.rows[1][4] != ""

    # "Escribiendo..." was sent while Groq worked, and before the reply
    first_presence = next(i for i, (e, _) in enumerate(gateway.calls) if e == "chat/sendPresence")
    first_text = next(i for i, (e, _) in enumerate(gateway.calls) if e == "message/sendText")
    assert first_presence < first_text
    assert gateway.presence_calls()[0] == {"number": PHONE, "presence": "composing", "delay": 20_000}

    # --- Still state null: data arrives piece by piece and is stored in the sheet.
    groq.onboarding_result = OnboardingLLMResponse(collected=OnboardingData(full_name="Lucía Fernández"))
    send("me llamo Lucía Fernández")
    assert worksheet.rows[1][:4] == [PHONE, "Lucía Fernández", "", ""]
    assert gateway.sent_texts()[-1] == ONBOARDING_MISSING_EMAIL_MESSAGE

    groq.onboarding_result = OnboardingLLMResponse(collected=OnboardingData(email="lucia@example.com"))
    send("lucia@example.com")
    assert worksheet.rows[1][:4] == [PHONE, "Lucía Fernández", "lucia@example.com", ""]
    assert len(worksheet.rows) == 2  # still one row for this person
    assert gateway.sent_texts()[-1] == PENDING_APPROVAL_MESSAGE

    # --- Still pending: no more LLM calls, just a reminder. Expenses are still refused.
    llm_calls_before = len(groq.onboarding_calls)
    send("gasté 700 en el súper")
    assert gateway.sent_texts()[-1] == PENDING_REMINDER_MESSAGE
    assert len(groq.onboarding_calls) == llm_calls_before
    assert expenses.saved == []

    # --- State 1: the administrator approves by hand in the sheet; the bot notices once its cache expires.
    worksheet.rows[1][3] = 1
    send("Almorcé sushi por 15.50 USD pagando con débito")
    assert expenses.saved == []  # still served from the in-memory snapshot (Sheets quota protection)
    assert gateway.sent_texts()[-1] == PENDING_REMINDER_MESSAGE

    clock.advance(61)
    send("Almorcé sushi por 15.50 USD pagando con débito")
    assert len(expenses.saved) == 1
    assert "Gasto Registrado con Éxito" in gateway.sent_texts()[-1]

    # --- State 2: the administrator rejects the user.
    worksheet.rows[1][3] = 2
    clock.advance(61)
    send("gasté 100 en café")
    assert gateway.sent_texts()[-1] == ACCESS_DENIED_MESSAGE
    assert len(expenses.saved) == 1


def test_a_stranger_messaging_repeatedly_causes_a_single_sheet_read(system):
    send, gateway, worksheet, _, groq, _ = system
    groq.onboarding_result = OnboardingLLMResponse()

    for _ in range(10):
        send("hola")

    # one read for the first lookup and one for the registration write; the rest is served from memory
    assert worksheet.reads == 2
    assert len(worksheet.rows) == 2 and len(worksheet.appends) == 1  # a single row, however much they insist
    assert len(gateway.sent_texts()) == 10


def test_an_edit_reported_by_the_apps_script_takes_effect_immediately_without_waiting_for_the_cache(system):
    send, gateway, worksheet, clock, groq, expenses = system
    groq.onboarding_result = OnboardingLLMResponse(
        collected=OnboardingData(full_name="Lucía Fernández", email="lucia@example.com")
    )
    send("soy Lucía Fernández, lucia@example.com")
    assert gateway.sent_texts()[-1] == PENDING_APPROVAL_MESSAGE

    worksheet.rows[1][3] = 1  # the administrator approves in the sheet...
    assert send.sheets_event(event="estado_changed", sheet="usuarios", phone=PHONE) == {"status": "INVALIDATED"}
    send("Almorcé sushi por 15.50 USD pagando con débito")  # ...and no 60 s wait is needed
    assert len(expenses.saved) == 1

    worksheet.rows[1][3] = 2  # a revocation is just as immediate
    send.sheets_event(event="estado_changed", sheet="usuarios", phone=PHONE)
    send("gasté 100 en café")
    assert gateway.sent_texts()[-1] == ACCESS_DENIED_MESSAGE
    assert len(expenses.saved) == 1


def test_an_approved_user_gets_their_report_and_only_their_own_data(system):
    send, gateway, worksheet, clock, groq, expenses = system
    groq.onboarding_result = OnboardingLLMResponse(
        collected=OnboardingData(full_name="Lucía Fernández", email="lucia@example.com")
    )
    send("soy Lucía Fernández, lucia@example.com")
    worksheet.rows[1][3] = 1
    send.sheets_event(phone=PHONE)
    send("Almorcé sushi por 15.50 USD pagando con débito")
    assert len(expenses.saved) == 1

    # someone else's expense exists in the same sheet: it must never reach Lucía's report
    from datetime import date
    from src.domain.enums import InputTypeEnum
    from src.domain.models import ExpenseRecord
    expenses.saved.append(ExpenseRecord(
        message_id="other", user_phone_hash="f" * 64, input_type=InputTypeEnum.TEXT, transaction_date=date(2026, 10, 19),
        amount=777777, currency="ARS", category="Otros", subcategory="Otros", payment_method="Efectivo",
        description="de otra persona", confidence_score=0.9, raw_text="x",
    ))

    send("Quiero mi resumen")

    assert send.publisher.url in gateway.sent_texts()[-1]
    title, rows, email = send.publisher.calls[0]
    assert email == "lucia@example.com"
    assert len(rows) == 1 and rows[0]["monto"] == 15.5
    assert groq.expense_calls == ["Almorcé sushi por 15.50 USD pagando con débito"]  # the request never reached the LLM
    assert len(expenses.saved) == 2  # nothing was registered for the request itself
