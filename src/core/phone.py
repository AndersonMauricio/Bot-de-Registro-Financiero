"""Phone number helpers shared by the WhatsApp parser, the users repository and logging."""

import re
from typing import Any


def normalize_phone(value: Any) -> str:
    """Return only the digits of a phone value (text, int or the whole float Sheets may hand back)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\D", "", str(value or ""))


def phone_key(value: Any) -> str:
    """Canonical key to match phones: Argentine mobiles appear both as 549... and 54... (13 vs 12 digits)."""
    digits = normalize_phone(value)
    if digits.startswith("549") and len(digits) == 13:
        return "54" + digits[3:]
    return digits


def mask_phone(value: Any) -> str:
    """Mask a phone for logs, keeping the country prefix and the last four digits."""
    digits = normalize_phone(value)
    return f"{digits[:3]}****{digits[-4:]}" if len(digits) > 7 else "****"
