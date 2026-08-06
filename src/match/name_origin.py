"""Name-origin gate — keep non-Slavic candidates out of the auto-dial pipeline.

The client dials only Ukrainian/Slavic candidates. Cyrillic OR Latin transliteration
are BOTH fine ("Ivanenko Petro" and "Коваль Оксана" are equally Slavic). Latin script
alone is NEVER a reason to reject. Names clearly of non-Slavic origin (African, Asian,
Arabic, Georgian, etc.) must be skipped BEFORE a card is created or a call is placed.

Safe-scenario rule: on any doubt — a mixed name, or one the model cannot place — SKIP.
A hard API error is the one exception: it falls back to ALLOW so a model outage never
silently drops the entire intake (the geo/age screens still apply downstream).
"""
from __future__ import annotations

import anthropic
import structlog

from src.common.settings import get_settings
from src.cost import usage

log = structlog.get_logger()

_SYSTEM = (
    "You decide whether a job candidate's full name is of Ukrainian/Slavic origin. "
    "SLAVIC = Ukrainian, Russian, Belarusian, Polish and similar names — whether "
    "written in Cyrillic OR Latin transliteration ('Ivanenko Petro', 'Коваль Оксана', "
    "'Bondarenko Olha' are all SLAVIC). Latin letters ALONE are NOT a reason to reject. "
    "FOREIGN = clearly non-Slavic origin: African, Asian, Arabic, Indian, Georgian, "
    "Turkic, Western-European, etc. ('Sunmisola Fatungase', 'Duruoha Yvonne', "
    "'Okoli Victor', 'Ndamati Albright Azunda', 'Klibadze Luka'). "
    "Judge the given name, the surname, AND their combination. "
    "Reply with ONE word only: SLAVIC, FOREIGN, or UNSURE. Use UNSURE for a mixed "
    "name (foreign given name + Slavic surname or vice versa) or anything you cannot "
    "confidently place."
)

# Per-process memo so the work.ua poller does not re-classify the same name every 5 min.
_cache: dict[str, bool] = {}


async def is_slavic_name(full_name: str) -> bool:
    """True → allowed into auto-dial. False → skip the candidate entirely (no card, no
    call). Foreign AND uncertain both resolve to skip (safe scenario). A hard API error
    resolves to allow, so a model outage cannot wipe out the whole intake."""
    name = (full_name or "").strip()
    if not name:
        return False  # no name to verify → skip
    if name in _cache:
        return _cache[name]
    s = get_settings()
    try:
        client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
        resp = await client.messages.create(
            model=s.anthropic_model_cheap,
            max_tokens=5,
            system=_SYSTEM,
            messages=[{"role": "user", "content": name}],
        )
        await usage.record(usage.NAME_ORIGIN, resp.usage, model=s.anthropic_model_cheap)
        verdict = ("".join(getattr(b, "text", "") for b in resp.content)).strip().upper()
        allowed = verdict.startswith("SLAVIC")
        _cache[name] = allowed
        if not allowed:
            log.info("name_origin.skip", name=name, verdict=verdict or "empty")
        return allowed
    except Exception as e:
        log.warning("name_origin.error_allow", name=name, error=str(e))
        return True  # infra error → do not drop the whole pipeline
