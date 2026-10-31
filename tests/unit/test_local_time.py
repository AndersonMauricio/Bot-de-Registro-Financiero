"""The bot's "today" must be the user's day: containers run in UTC and "hoy/ayer" would be off."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from src.core import clock
from src.core.config import settings


def test_local_today_follows_the_configured_timezone(monkeypatch):
    monkeypatch.setattr(settings, "TIMEZONE", "Asia/Tokyo")

    assert clock.local_today() == datetime.now(ZoneInfo("Asia/Tokyo")).date()


def test_local_today_does_not_use_the_server_clock_date(monkeypatch):
    """With a timezone far from UTC, the local date differs from `date.today()` for part of the day."""
    monkeypatch.setattr(settings, "TIMEZONE", "Pacific/Kiritimati")  # UTC+14

    expected = (datetime.now(timezone.utc) + timedelta(hours=14)).date()

    assert clock.local_today() == expected


def test_an_unknown_timezone_falls_back_to_utc_minus_three(monkeypatch):
    """Same fallback the storage layer uses, so a typo in TIMEZONE never breaks the pipeline."""
    monkeypatch.setattr(settings, "TIMEZONE", "Not/AZone")

    assert clock.local_now().utcoffset() == timedelta(hours=-3)
