from __future__ import annotations

REGION_ALIASES: dict[str, str] = {
    "київська обл.": "Київська",
    "київська область": "Київська",
    "kyiv region": "Київська",
    "м.київ": "м. Київ",
    "київ": "м. Київ",
    "kyiv": "м. Київ",
    "житомирська обл.": "Житомирська",
    "вінницька обл.": "Вінницька",
    "хмельницька обл.": "Хмельницька",
    "тернопільська обл.": "Тернопільська",
    "львівська обл.": "Львівська",
    "івано-франківська обл.": "Івано-Франківська",
    "закарпатська обл.": "Закарпатська",
    "чернівецька обл.": "Чернівецька",
    "рівненська обл.": "Рівненська",
    "волинська обл.": "Волинська",
    "черкаська обл.": "Черкаська",
    # Blacklisted (auto-mapped for filter to catch)
    "сумська обл.": "Сумська",
    "запорізька обл.": "Запорізька",
    "запоріжжя": "Запоріжжя",
    "херсонська обл.": "Херсонська",
    "херсон": "Херсон",
    "донецька обл.": "Донецька",
    "донецька область": "Донецька",
    "суми": "Суми",
}


def normalize_region(raw: str) -> str:
    if not raw:
        return ""
    key = raw.strip().lower()
    if key in REGION_ALIASES:
        return REGION_ALIASES[key]
    return raw.strip()


def is_region_allowed(region: str, allowed: set[str], blocked: set[str]) -> bool:
    norm = normalize_region(region)
    if norm in blocked:
        return False
    return norm in allowed
