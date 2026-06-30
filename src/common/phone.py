from __future__ import annotations

import phonenumbers
from phonenumbers import NumberParseException


def normalize_phone(raw: str, default_region: str = "UA") -> str | None:
    """Normalize a phone number to E.164 format (+380XXXXXXXXX).

    Returns None if invalid.
    """
    if not raw:
        return None
    raw = raw.strip()
    raw = raw.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def is_ukrainian(phone_e164: str) -> bool:
    return phone_e164.startswith("+380")
