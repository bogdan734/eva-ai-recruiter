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
    # Cities → their oblast. Єва asks "в якому місті ви проживаєте?", so the
    # answer is almost always a CITY, while the whitelist is written in OBLASTS.
    # Without these, "Львів" and "Одеса" were rejected even though Львівська and
    # Одеська are allowed — the filter compared a city against an oblast list.
    # Found 2026-08-05 after Єва turned away a candidate from Дніпро.
    "дніпро": "Дніпропетровська",
    "дніпропетровськ": "Дніпропетровська",
    "днепр": "Дніпропетровська",
    "львів": "Львівська",
    "львов": "Львівська",
    "одеса": "Одеська",
    "одесса": "Одеська",
    "житомир": "Житомирська",
    "хмельницький": "Хмельницька",
    "тернопіль": "Тернопільська",
    "івано-франківськ": "Івано-Франківська",
    "ивано-франковск": "Івано-Франківська",
    "ужгород": "Закарпатська",
    "мукачево": "Закарпатська",
    "чернівці": "Чернівецька",
    "черновцы": "Чернівецька",
    "рівне": "Рівненська",
    "ровно": "Рівненська",
    "луцьк": "Волинська",
    "луцк": "Волинська",
    "черкаси": "Черкаська",
    "черкассы": "Черкаська",
    "кривий ріг": "Дніпропетровська",
    "кривой рог": "Дніпропетровська",
    "камʼянське": "Дніпропетровська",
    "камянське": "Дніпропетровська",
    # Cities in blocked oblasts, so the filter catches them by name too.
    "вінниця": "Вінницька",
    "винница": "Вінницька",
}


# Job boards spell the oblast out in full ("Львівська область"), the whitelist
# in .env uses the short form ("Львівська"). Without this strip every robota.ua
# candidate would fail the geo filter with a region that is actually allowed.
_OBLAST_SUFFIXES = (" область", " обл.", " обл", " oblast", " region")


def normalize_region(raw: str) -> str:
    if not raw:
        return ""
    key = raw.strip().lower()
    if key in REGION_ALIASES:
        return REGION_ALIASES[key]
    for suffix in _OBLAST_SUFFIXES:
        if key.endswith(suffix):
            base = raw.strip()[: -len(suffix)].strip()
            base_key = base.lower()
            if base_key in REGION_ALIASES:
                return REGION_ALIASES[base_key]
            return base
    return raw.strip()


def is_region_allowed(region: str, allowed: set[str], blocked: set[str]) -> bool:
    norm = normalize_region(region)
    if norm in blocked:
        return False
    return norm in allowed
