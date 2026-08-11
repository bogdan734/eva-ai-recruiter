"""Tell a dead line apart from a candidate who did not pick up.

Between 05.08 and 11.08.2026 every outbound call came back from Stream Telecom
as `error-providerfault-outbound-sip-403-forbidden`: 77 calls, 27 distinct
destinations, `cost: 0`, `messages: 0` — not one phone ever rang. The dispatcher
had no way to know that. To it a call that produced nothing is a call that
failed, so it did what it always does: spent an attempt. Four days later twelve
real candidates were sitting in `unreachable`, dispositioned as people who would
not answer, and the funnel looked like it had done its job.

Two ideas here, and the split between them matters:

  **provider_fault** — the carrier refused to place the call at all (403, 503,
  a generic providerfault). Says nothing whatsoever about the candidate. Must
  not cost an attempt, must not move anyone to `unreachable`, and if it keeps
  happening the whole outbound run should stop rather than grind the base down.

  **dead_number** — the carrier reached the network and the network said this
  number does not exist (404, 604, 410, 484). That IS about this candidate, and
  redialling will fail identically forever, so stop early instead of burning the
  full attempt budget on a number that cannot ring.

Everything else — busy, no answer, a real conversation — keeps the existing
behaviour untouched.

The breaker needs BOTH a streak and several distinct numbers before it trips:
one unlucky destination is not an outage, and pausing a client's recruiting is
not something to do on thin evidence.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import structlog

from src.common.state import state_dir

log = structlog.get_logger()

STATE_NAME = "line_health.json"

# Substring matched against Vapi's `endedReason`, lowercased. Vapi spells these
# `call.in-progress.error-providerfault-outbound-sip-403-forbidden` and friends;
# matching on fragments keeps working when they add a new prefix.
PROVIDER_FAULT_MARKERS = (
    "providerfault",
    "sip-403",
    "403-forbidden",
    "sip-503",
    "503-service-unavailable",
)

# The network answered "no such subscriber". Retrying is pointless.
# NOT included: `failed-to-connect`, which we have seen from both a sick trunk
# and a genuinely unreachable handset — too ambiguous to disposition anyone on.
DEAD_NUMBER_MARKERS = (
    "sip-404",
    "404-not-found",
    "sip-604",
    "604-does-not-exist",
    "does-not-exist-anywhere",
    "sip-410",
    "410-gone",
    "484-address-incomplete",
)


def classify(ended_reason: str | None) -> str:
    """`provider_fault` | `dead_number` | `other`."""
    r = (ended_reason or "").strip().lower()
    if not r:
        return "other"
    # Dead-number codes are checked FIRST and the order is load-bearing: Vapi
    # labels those `error-providerfault-outbound-sip-404-not-found` too, so a
    # provider-first test reads "no such subscriber" as "the trunk is down" and
    # a handful of stale numbers would pause the whole outbound run.
    if any(m in r for m in DEAD_NUMBER_MARKERS):
        return "dead_number"
    if any(m in r for m in PROVIDER_FAULT_MARKERS):
        return "provider_fault"
    return "other"


def _streak_needed() -> int:
    return max(1, int(os.getenv("LINE_BREAKER_STREAK") or 5))


def _numbers_needed() -> int:
    return max(1, int(os.getenv("LINE_BREAKER_MIN_NUMBERS") or 3))


def _path() -> Path:
    return state_dir() / STATE_NAME


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing or corrupt file just means "healthy"
        return {}


def _save(data: dict) -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — never let bookkeeping kill a call
        log.warning("line_health.save_failed", error=str(e))


def record_success() -> None:
    """Any call the carrier actually placed clears the streak."""
    data = _load()
    if data.get("streak"):
        log.info("line_health.recovered", streak_was=data.get("streak"))
    _save({"streak": 0, "numbers": [], "tripped_at": data.get("tripped_at")})


def record_provider_fault(phone: str | None, reason: str | None) -> bool:
    """Count one carrier refusal. True when this one trips the breaker."""
    data = _load()
    streak = int(data.get("streak") or 0) + 1
    numbers = list(data.get("numbers") or [])
    if phone and phone not in numbers:
        numbers.append(phone)
    data = {
        "streak": streak,
        "numbers": numbers[-20:],
        "last_reason": (reason or "")[:120],
        "last_at": datetime.utcnow().isoformat(timespec="seconds"),
        "tripped_at": data.get("tripped_at"),
    }
    trip = streak >= _streak_needed() and len(numbers) >= _numbers_needed()
    if trip and not data.get("tripped_at"):
        data["tripped_at"] = data["last_at"]
    _save(data)
    log.warning(
        "line_health.provider_fault",
        streak=streak, distinct_numbers=len(numbers), reason=reason, phone=phone,
    )
    return bool(trip)


def pause_calls(reason: str) -> None:
    """Flip the same shared flag the admin bot's pause button writes.

    Written straight to STATE_PATH rather than through `src.bot.admin`, which
    would drag aiogram into the orchestrator. `calls_paused()` re-reads the file
    on every check, so the dispatcher sees this on its next slot.
    """
    path = Path(os.getenv("STATE_PATH") or "/tmp/ai_recruiter_state.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:  # noqa: BLE001
        data = {}
    if data.get("calls_paused"):
        return
    data["calls_paused"] = True
    data["paused_by"] = "line_breaker"
    data["paused_reason"] = reason[:200]
    data["paused_at"] = datetime.utcnow().isoformat(timespec="seconds")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.error("line_health.calls_paused", reason=reason)
    except Exception as e:  # noqa: BLE001
        log.warning("line_health.pause_failed", error=str(e))


async def alert_admins(text: str) -> None:
    """Best-effort Telegram shout. A breaker nobody hears about is not a breaker."""
    token = (os.getenv("TG_REPORT_BOT_TOKEN") or "").strip()
    raw = (os.getenv("TG_ADMIN_CHAT_IDS") or "").strip()
    if not token or not raw:
        return
    import httpx

    for chat in [c.strip() for c in raw.split(",") if c.strip()]:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
                )
        except Exception as e:  # noqa: BLE001
            log.warning("line_health.alert_failed", chat=chat, error=str(e))
