"""Detects when an approved user refers to an already registered expense without quoting it.

Corrections are applied by quoting the bot's confirmation (the message that carries the expense id).
When the user forgets to quote ("me equivoqué, eran 155"), the message must not be registered as a new
expense nor answered with a technical validation error: the bot asks for the quote instead.

Deterministic on purpose (no LLM call), like the report-intent detection.
"""

import re
import unicodedata
from typing import Optional

# A correction is a short remark about a record; long texts are new expenses or explanations
MAX_WORDS = 25

# Expressions that only make sense when talking about something already registered
_CORRECTION_CUE = re.compile(
    r"\b("
    r"me\s+equivoq\w*|equivoq\w*|"
    r"me\s+confund\w*|confund\w*|"
    r"en\s+realidad|"
    r"corrij\w*|correg\w*|corrijo|"
    r"quise\s+decir|"
    r"no\s+(?:eran|era|fue|son|es)\b|"
    r"me\s+di\s+cuenta|"
    r"estaba\s+mal|esta\s+mal|"
    r"mal\s+(?:el|la|los|las)\s+\w+"
    r")"
)

# A message that registers a new expense always talks about paying/spending
_SPENDING_CUE = re.compile(r"\b(gaste|pague|compre|saque|abone|me\s+costo|cobre|transferi|deposite)\b")


def _normalize(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^\w$€\s]", " ", ascii_text.lower())


def is_correction_reference(text: str) -> bool:
    """True when the message seems to correct an already registered expense without quoting it."""
    normalized = _normalize(text or "")
    words = normalized.split()
    if not words or len(words) > MAX_WORDS:
        return False
    if not _CORRECTION_CUE.search(normalized):
        return False
    # "gasté 155, en realidad tenía descuento" describes a new expense, not a correction of a stored one
    return not (_SPENDING_CUE.search(normalized) and "equivoq" not in normalized)


# Explicit currency mentions: only those may change the currency of a stored expense
_CURRENCY_MENTION = re.compile(
    r"\b(ars|usd|u\$s|us\$|dolar(?:es)?|pesos?|euros?|eur|reales|brl|clp|cop|mxn|soles|guaranies)\b|[$€]"
)
# Explicit date mentions: only those may change the date of a stored expense
_DATE_MENTION = re.compile(
    r"\b(ayer|antier|anteayer|hoy|manana|"
    r"lunes|martes|miercoles|jueves|viernes|sabado|domingo|"
    r"semana\s+pasada|mes\s+pasado|el\s+\d{1,2})\b"
    r"|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
    r"|\d{4}-\d{2}-\d{2}"
)


def mentions_currency(text: str) -> bool:
    """True when the user says which currency they mean (so the stored one may be replaced)."""
    return bool(_CURRENCY_MENTION.search(_normalize(text or "")))


def mentions_date(text: str) -> bool:
    """True when the user says which date they mean (so the stored one may be replaced)."""
    return bool(_DATE_MENTION.search(_normalize(text or "")))


# "la opción 2", "el gasto 2", "el 2", "2)", "2 -", "2:" or just "2"
_SELECTION_WITH_NOUN = re.compile(
    r"^\s*(?:(?:el|la|los|las)\s+)?(?:opci[oó]n|n[uú]mero|gasto|[íi]tem|item)\s+(\d{1,2})(?!\d)"
    r"\s*[).:,;\-]?\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_SELECTION_WITH_ARTICLE = re.compile(
    r"^\s*(?:el|la|los|las)\s+(\d{1,2})(?!\d)\s*[).:,;\-]?\s*(.*)$", re.IGNORECASE | re.DOTALL
)
_SELECTION_WITH_SEPARATOR = re.compile(r"^\s*(\d{1,2})(?!\d)\s*[).:,;\-]\s*(.*)$", re.DOTALL)
_SELECTION_ONLY = re.compile(r"^\s*(\d{1,2})(?!\d)\s*$")


def parse_selection(text: str) -> tuple[Optional[int], str]:
    """Interpret a reply to a numbered list as (1-based option, remaining correction text).

    Returns (None, text) when the message does not name an option, so a plain correction or a new
    expense ("2 cafés y 3 medialunas") is never mistaken for a choice.
    """
    raw = text or ""
    for pattern in (_SELECTION_WITH_NOUN, _SELECTION_WITH_ARTICLE, _SELECTION_WITH_SEPARATOR):
        match = pattern.match(raw)
        if match:
            return int(match.group(1)), match.group(2).strip()
    match = _SELECTION_ONLY.match(raw)
    if match:
        return int(match.group(1)), ""
    return None, raw.strip()
