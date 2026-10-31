"""Local clock: the bot's "today" is the user's day, not the server's (containers run in UTC)."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from src.core.config import settings


def local_timezone() -> timezone | ZoneInfo:
    """Configured timezone, with the same UTC-3 fallback the storage layer already uses."""
    try:
        return ZoneInfo(settings.TIMEZONE)
    except Exception:
        return timezone(timedelta(hours=-3))


def local_now() -> datetime:
    """Current local datetime in `TIMEZONE`."""
    return datetime.now(local_timezone())


def local_today() -> date:
    """Current local date in `TIMEZONE`: what "hoy" and "ayer" mean for the user."""
    return local_now().date()
