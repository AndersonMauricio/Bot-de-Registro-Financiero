"""Detects when an approved user asks for their expense report instead of registering an expense.

Deterministic on purpose (no LLM call): a keyword search ("resumen", "reporte", "mis gastos", "cuánto gasté"...),
guarded so that real expenses are never taken for requests. An expense always carries an amount and a spending
verb, a report request never does.
"""

import re
import unicodedata
from src.services.edit_intent import is_correction_reference

# A request is a short remark; longer texts are narratives that merely mention one of the keywords
MAX_WORDS = 25

# Keywords of a report request, on normalized text (lowercase, no accents)
_REPORT_KEYWORDS = re.compile(
    r"\b("
    r"resumen(?:es)?|reportes?|informes?|extractos?|planillas?|"
    r"mis\s+gastos|"
    r"gastos\s+del\s+(?:mes|ano|dia|semana)"
    r")\b"
)
# "cuánto gasté", "cuánto gasto este mes", "cuánto llevo gastado": the question itself is the request
_HOW_MUCH_SPENT = re.compile(r"\bcuant[oa]\s+(?:\w+\s+){0,2}?(?:gaste|gasto|gastamos|gastado)\b")
# Spending verbs: with one of these (and no request verb) the message describes a purchase ("pagué el resumen")
_SPENDING_CUE = re.compile(r"\b(gaste|pague|compre|abone|saque|transferi|deposite|cobre|me\s+costo)\b")
_REQUEST_CUE = re.compile(
    r"\b(quier\w*|quisier\w*|queri\w*|dame|dam\w*|dar|pasa\w*|mand\w*|envi\w*|necesit\w*|gener\w*|arma\w*|"
    r"mostr\w*|ver|pued\w*|pod\w*|solicit\w*|pedir|pido)\b"
)
# Amounts: currency signs, thousands separators, a number followed by a currency word
_AMOUNT = re.compile(
    r"[$€]|\b\d{1,3}(?:[.,]\d{3})+\b|\b\d+(?:[.,]\d+)?\s*(?:pesos?|ars|usd|u\$s|dolar(?:es)?|euros?|lucas?|k|mil)\b"
)
# Numbers that name a period, not an amount: years ("octubre 2026") and spans ("los últimos 30 días")
_PERIOD = re.compile(r"\b(?:19|20)\d{2}\b|\b\d+\s*(?:dias?|semanas?|mes(?:es)?|anos?)\b")
_BARE_NUMBER = re.compile(r"\d")


def _normalize(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^\w$€.,\s]", " ", ascii_text.lower())


def is_report_request(text: str) -> bool:
    """True when the message asks for a summary/report of the user's own expenses."""
    normalized = _normalize(text or "")
    words = normalized.split()
    if not words or len(words) > MAX_WORDS:
        return False
    # Any amount, even a bare "500", means the user is describing an expense
    if _AMOUNT.search(normalized) or _BARE_NUMBER.search(_PERIOD.sub(" ", normalized)):
        return False
    # "me equivoqué en mis gastos de ayer" corrects a record, it does not ask for a report
    if is_correction_reference(text or ""):
        return False
    if _HOW_MUCH_SPENT.search(normalized):
        return True
    if not _REPORT_KEYWORDS.search(normalized):
        return False
    # "pagué el resumen de la tarjeta" is a purchase; "pasame el resumen de lo que gasté" is a request
    return not _SPENDING_CUE.search(normalized) or bool(_REQUEST_CUE.search(normalized))
