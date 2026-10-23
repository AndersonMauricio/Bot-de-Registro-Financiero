"""Detects when an approved user asks for their expense report instead of registering an expense.

Deterministic on purpose (no LLM call): an expense message always carries an amount, a report request never does.
"""

import re
import unicodedata

MAX_WORDS = 15

_REPORT_NOUN = re.compile(r"\b(resumen|reporte|informe|extracto|planilla)\b")
# "resumen de gastos", "reporte de mis gastos": a request even without an explicit verb
_REPORT_OF_EXPENSES = re.compile(r"\b(resumen|reporte|informe|extracto)\s+(de\s+)?(mis\s+|los\s+|todos\s+mis\s+)?gastos\b")
_REQUEST_CUE = re.compile(
    r"\b(quier\w*|quisier\w*|queri\w*|dame|dam\w*|dar|pasa\w*|mand\w*|envi\w*|necesit\w*|gener\w*|arma\w*|"
    r"sac\w*|mostr\w*|ver|pued\w*|pod\w*|solicit\w*|pedir|pido)\b"
)
# Amounts: currency signs, thousands separators, a number followed by a currency word. Bare years are not amounts.
_AMOUNT = re.compile(
    r"[$€]|\b\d{1,3}(?:[.,]\d{3})+\b|\b\d+(?:[.,]\d+)?\s*(?:pesos?|ars|usd|u\$s|dolar(?:es)?|euros?|lucas?|k|mil)\b"
)


def _normalize(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^\w$€.,\s]", " ", ascii_text.lower())


def is_report_request(text: str) -> bool:
    """True when the message asks for a summary/report of the user's own expenses."""
    normalized = _normalize(text or "")
    words = normalized.split()
    if not words or len(words) > MAX_WORDS:
        return False
    if _AMOUNT.search(normalized):
        return False
    if _REPORT_OF_EXPENSES.search(normalized):
        return True
    if not _REPORT_NOUN.search(normalized):
        return False
    # A noun on its own ("resumen", "mi reporte") or together with a request verb ("quiero mi resumen")
    return len(words) <= 3 or bool(_REQUEST_CUE.search(normalized))
