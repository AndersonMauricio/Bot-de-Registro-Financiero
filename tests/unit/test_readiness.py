"""Configuration checks behind /health/ready: name what is missing, never expose values."""

from fastapi.testclient import TestClient
from src.api.v1.dependencies import get_settings
from src.core.config import Settings
from src.core.readiness import missing_configuration
from src.main import app

READY_URL = "/api/v1/health/ready"


def make_settings(**overrides) -> Settings:
    """Settings with every required value set; `_env_file=None` never reads the developer's `.env`."""
    values = {
        "GROQ_API_KEY": "groq-key",
        "EVOLUTION_API_KEY": "evolution-key",
        "EVOLUTION_INSTANCE": "an-instance",
        "EVOLUTION_WEBHOOK_SECRET": "evolution-secret",
        "SPREADSHEET_ID": "spreadsheet-id",
        "GCP_SERVICE_ACCOUNT_B64": "e30=",
        "STORAGE_BACKEND": "sheets",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_a_fully_configured_bot_is_ready():
    assert missing_configuration(make_settings()) == []


def test_the_missing_settings_are_named():
    missing = missing_configuration(make_settings(GROQ_API_KEY="", SPREADSHEET_ID=""))

    assert set(missing) == {"GROQ_API_KEY", "SPREADSHEET_ID"}


def test_an_unknown_storage_backend_is_reported():
    assert "STORAGE_BACKEND" in missing_configuration(make_settings(STORAGE_BACKEND="mysql"))


def test_google_credentials_are_required_whichever_backend_stores_the_expenses(monkeypatch, tmp_path):
    """The users tab always lives in Sheets, so switching to postgres does not remove the requirement."""
    monkeypatch.chdir(tmp_path)  # no config/credentials to auto-discover
    settings = make_settings(STORAGE_BACKEND="postgres", GCP_SERVICE_ACCOUNT_B64=None)

    assert "GOOGLE_APPLICATION_CREDENTIALS o GCP_SERVICE_ACCOUNT_B64" in missing_configuration(settings)


def test_the_ready_endpoint_answers_503_and_names_what_is_missing():
    app.dependency_overrides[get_settings] = lambda: make_settings(GROQ_API_KEY="")
    try:
        response = TestClient(app).get(READY_URL)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "missing": ["GROQ_API_KEY"]}


def test_the_ready_endpoint_answers_200_when_everything_is_set():
    app.dependency_overrides[get_settings] = lambda: make_settings()
    try:
        response = TestClient(app).get(READY_URL)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
