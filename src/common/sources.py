"""Single registry of candidate sources.

Everything that needs to say "where did this person come from" reads from here:
the voice script, the Telegram persona, the CRM card, the daily report and the
admin menu. Adding robota.ua (or any other board) means one entry below —
not a hunt through hardcoded "work.ua" strings.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    key: str          # canonical short key stored in candidates.source
    label: str        # how a recruiter sees it in reports/menu
    spoken: str       # how Eva pronounces it on a call (TTS-safe)
    written: str      # how Eva writes it in Telegram
    origin: str       # full sentence answering "звідки у вас мій номер?"


SOURCES: dict[str, Source] = {
    "workua": Source("workua", "work.ua", "ворк юей", "work.ua",
                     "Ви залишали резюме на ворк юей — звідти ваш контакт."),
    "robotaua": Source("robotaua", "robota.ua", "робота юей", "robota.ua",
                       "Ви залишали резюме на робота юей — звідти ваш контакт."),
    "olx": Source("olx", "OLX", "оел ікс", "OLX",
                  "Ви залишали резюме на оел ікс — звідти ваш контакт."),
    "inbound": Source("inbound", "вхідний дзвінок", "ви телефонували нам", "ваш дзвінок нам",
                      "Ви телефонували нам раніше — звідти ваш контакт."),
    "manual": Source("manual", "ручний імпорт", "наша база", "наша база",
                     "Ваш контакт є в нашій базі кандидатів."),
    "telegram": Source("telegram", "Telegram", "телеграм", "Telegram",
                       "Ви писали нам у телеграм — звідти ваш контакт."),
}

UNKNOWN = Source("unknown", "невідомо", "наша база", "наша база",
                 "Ваш контакт є в нашій базі кандидатів.")

# raw values already present in the database / used by integrations
_ALIASES: dict[str, str] = {
    "workua_response_send": "workua",
    "workua_api": "workua",
    "workua_response": "workua",
    "work_ua": "workua",
    "robota_ua": "robotaua",
    "robota": "robotaua",
    "inbound_call": "inbound",
    "tg_test_call": "manual",
    "prod_number_check": "manual",
    "manual_import": "manual",
}


def normalize(raw: str | None) -> str:
    """Map whatever is stored in candidates.source onto a canonical key."""
    if not raw:
        return UNKNOWN.key
    key = raw.strip().lower()
    if key in SOURCES:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    for prefix, canonical in (("workua", "workua"), ("work_ua", "workua"),
                              ("robota", "robotaua"), ("olx", "olx"),
                              ("inbound", "inbound")):
        if key.startswith(prefix):
            return canonical
    return UNKNOWN.key


def get(raw: str | None) -> Source:
    return SOURCES.get(normalize(raw), UNKNOWN)


def label(raw: str | None) -> str:
    """Human-readable, for reports and the admin menu."""
    return get(raw).label


def spoken(raw: str | None) -> str:
    """TTS-safe wording for the voice script ("резюме на ворк юей")."""
    return get(raw).spoken


def origin(raw: str | None) -> str:
    """Ready-made answer to "звідки у вас мій номер?"."""
    return get(raw).origin


def written(raw: str | None) -> str:
    """Plain wording for Telegram messages."""
    return get(raw).written
