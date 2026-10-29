"""Pending "which expense of this batch?" selection.

A message that registers several expenses produces ONE confirmation, so quoting it is ambiguous.
The bot lists the candidates numbered and remembers them for a short while, so the next message
("el 2 eran 6000") can name the row to correct.
"""

from dataclasses import dataclass
from typing import Optional
from src.core.config import settings
from src.infrastructure.cache.ttl_cache import TTLCache


@dataclass
class PendingSelection:
    """Numbered candidates the user still has to choose from, plus the message that listed them."""
    candidates: list[dict]
    prompt_message_id: Optional[str] = None
    # Set once the user picked an option without giving the new value: the next message is that value
    awaiting_value: Optional[int] = None


def amount_label(row: dict) -> str:
    """Amount of a stored row as `20,000.00 ARS` (the raw cell may be a number or text)."""
    try:
        amount = float(str(row.get("monto") or "").replace(",", "."))
    except ValueError:
        return f"{row.get('monto') or '—'} {row.get('moneda') or ''}".strip()
    return f"{amount:,.2f} {str(row.get('moneda') or '').strip()}".strip()


def describe(row: dict) -> str:
    """One line describing a stored expense: `Alimentación (Pochoclos) — 20,000.00 ARS`."""
    category = str(row.get("categoria") or "").strip()
    subcategory = str(row.get("subcategoria") or "").strip()
    heading = f"{category} ({subcategory})" if category and subcategory else category or subcategory
    return f"{heading or 'Gasto'} — {amount_label(row)}"


def numbered(candidates: list[dict]) -> str:
    """`1. …` / `2. …` lines, in the order the user saw them."""
    return "\n".join(f"{index}. {describe(row)}" for index, row in enumerate(candidates, start=1))


class PendingSelectionStore:
    """Per-user pending selections, expiring on their own (the process keeps no other conversation state)."""

    def __init__(self, ttl_seconds: Optional[float] = None, cache: Optional[TTLCache] = None):
        self._cache: TTLCache = cache or TTLCache(ttl_seconds or settings.PENDING_SELECTION_TTL_SECONDS)

    def put(self, user_key: str, candidates: list[dict]) -> PendingSelection:
        pending = PendingSelection(candidates=list(candidates))
        self._cache.set(user_key, pending)
        return pending

    def get(self, user_key: str) -> Optional[PendingSelection]:
        return self._cache.get(user_key)

    def clear(self, user_key: str) -> None:
        self._cache.delete(user_key)
