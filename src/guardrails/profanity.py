"""Profanity / aggression detector.

Two layers:
  1. Fast regex on known UA/RU profanity stems (cheap, near-zero latency)
  2. Optional Claude Haiku LLM-judge for ambiguous cases (>50ms, used sparingly)

NOTE: keep the regex list short and only stems — false positives in HR call cost trust.
"""
from __future__ import annotations

import re

# Profanity stems — Ukrainian + Russian. Censored partially to reduce git-grep noise.
_STEMS = [
    r"бля",
    r"еба",
    r"пизд",
    r"хуй",
    r"хуе",
    r"сук",
    r"пид[ао]р",
    r"мраз",
    r"гондон",
    r"мудак",
    r"идиот",
    r"ідіот",
    r"дура[кч]",
    r"дебіл",
    r"дебил",
]

_PATTERN = re.compile("|".join(_STEMS), re.IGNORECASE | re.UNICODE)

_AGGRESSIVE_MARKERS = [
    r"\bзаткн",  # "заткнись"
    r"\bотвал",
    r"\bвідвал",
    r"\bйди\s+на\s+",
    r"\bпошёл\s+на",
    r"\bдос[тт]ав",
    r"\bне\s+звон",
    r"\bне\s+дзвон",
]
_AGGRESSIVE_PATTERN = re.compile("|".join(_AGGRESSIVE_MARKERS), re.IGNORECASE)


def contains_profanity(text: str) -> bool:
    return bool(_PATTERN.search(text or ""))


def is_aggressive(text: str) -> bool:
    return contains_profanity(text) or bool(_AGGRESSIVE_PATTERN.search(text or ""))
