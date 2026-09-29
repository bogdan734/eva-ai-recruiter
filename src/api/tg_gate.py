"""May Eva answer this person in Telegram? Decided by the live CRM card.

Until 28.09 the answer came from our own status: `manager_review` meant "a
recruiter owns them" and Eva went quiet. But every applicant from a job board
gets `manager_review` the moment they apply, long before any recruiter looks at
them — so Eva invited them to chat and then never answered. The owner's rule
since 28.09: Eva leads the conversation to the end, and the recruiter gets the
person only once they fully qualify. So the card decides:

  * no card, or the card was deleted          → Eva talks (a new card appears
                                                 once the chat is real)
  * Новий, В роботі, Недозвін                  → Eva talks
  * Відібрано and the recruiter's stages       → Eva is quiet, recruiters are told
  * already dispositioned (Не ЦА, …)           → Eva is quiet, nobody is pinged

Stage meaning follows the client's rule of 02.09, the same one calls use:
«Відібрано» is Eva's finished selection waiting for the recruiter, «В роботі»
is Eva's unfinished work.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import select

from src.common.db import session_scope
from src.common.models import Candidate
from src.common.phone import normalize_phone

log = structlog.get_logger()

# Statuses where only the card can say whether a human has taken over.
_ASK_THE_CARD = {"manager_review", "interview_scheduled"}

# Funnel 1 stages. Anything else — another funnel's stage — counts as a
# recruiter's: quiet and tell them, never guess.
EVA_WORKING = {1, 3, 31}        # Новий, В роботі (her unfinished work), Недозвін
FINAL = {5, 32, 33, 34, 82}     # Не підтвердили, Не актуально, Не підходить, Не ЦА, Резерв


@dataclass
class GateDecision:
    engage: bool
    notify: bool
    why: str
    stage: int | None = None


def _status(cand: Candidate | None) -> str:
    raw = cand.status if cand else ""
    return str(getattr(raw, "value", raw) or "")


async def decide(cand: Candidate | None, crm) -> GateDecision:
    """engage: Eva answers. notify: Eva is quiet and a recruiter should read it."""
    from src.bot.admin import calls_paused

    if calls_paused():  # Eva is stopped (/pause): people get a human instead
        return GateDecision(False, True, "paused")
    if cand is None:
        return GateDecision(True, False, "unknown_person")
    status = _status(cand)
    if status == "closed":
        return GateDecision(False, False, "closed")
    if status not in _ASK_THE_CARD:
        return GateDecision(True, False, "eva_status")
    if not cand.keycrm_lead_id:
        return GateDecision(True, False, "no_card")
    try:
        stage = await crm.live_card_status(int(cand.keycrm_lead_id))
    except Exception as e:  # noqa: BLE001 — cannot see the card: keep the old, quiet way
        log.warning("tg_gate.card_unknown", lead_id=cand.keycrm_lead_id, error=str(e))
        return GateDecision(False, True, "card_unknown")
    if stage is None:
        return GateDecision(True, False, "card_deleted")
    if stage in EVA_WORKING:
        return GateDecision(True, False, "eva_stage", stage)
    if stage in FINAL:
        return GateDecision(False, False, "final_stage", stage)
    return GateDecision(False, True, "recruiter_stage", stage)


async def find_candidate(peer_id: str, phone: str | None) -> Candidate | None:
    """Same resolution as tg-outcome: real phone if known, else the tg<peer> surrogate."""
    keys = [f"tg{peer_id}"[:20]]
    if phone:
        try:
            norm = normalize_phone(phone)
        except Exception:  # noqa: BLE001
            norm = None
        if norm:
            keys.insert(0, norm)
    async with session_scope() as session:
        return (await session.execute(
            select(Candidate).where(Candidate.phone_e164.in_(keys))
        )).scalars().first()
