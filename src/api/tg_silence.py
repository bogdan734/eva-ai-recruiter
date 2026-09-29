"""A Telegram chat that went quiet: remind, let the card go, pick it up again.

The owner's rule of 29.09: a candidate who stops answering Eva in Telegram does
not keep a «В роботі» card forever. After two silent days Eva sends one
reminder; after three more the card goes to «Не актуально»; someone who writes
back after that is Eva's again. The userbot keeps the clock (it has the chat);
this module answers for the card, which only the CRM knows.

Only a card in «В роботі» is touched: anywhere else a person has decided, and a
promised callback belongs to the dialer, not to the chat.
"""
from __future__ import annotations

import structlog

from src.api.tg_gate import find_candidate
from src.common.crm import get_crm
from src.common.db import session_scope
from src.common.models import Candidate, CandidateStatus

log = structlog.get_logger()

IN_WORK = 3        # «В роботі» — Eva's unfinished work
NOT_ACTUAL = 32    # «Не актуально»


def _value(status) -> str:
    return str(getattr(status, "value", status) or "")


async def _set_status(cand_id: int, status: CandidateStatus) -> None:
    async with session_scope() as session:
        row = await session.get(Candidate, cand_id)
        if row is not None:
            row.status = status


async def handle_tg_silence(*, peer_id: str, phone: str | None, action: str, crm=None) -> dict:
    """action: "check" (may Eva remind?), "close" (let the card go), "reopen"."""
    from src.bot.admin import calls_paused

    if calls_paused():  # Eva is stopped: no reminders, no closing, no reopening
        return {"ok": True, "eligible": False, "closed": False, "reopened": False, "why": "paused"}
    cand = await find_candidate(peer_id, phone)
    if cand is None or not cand.keycrm_lead_id:
        return {"ok": True, "eligible": False, "closed": False, "reopened": False, "why": "no_card"}
    lead_id = int(cand.keycrm_lead_id)
    status = _value(cand.status)
    kc = crm or get_crm()
    try:
        stage = await kc.live_card_status(lead_id)
        dialer_owns = status == "in_call_queue" and cand.callback_at is not None
        eligible = stage == IN_WORK and not dialer_owns

        if action == "check":
            return {"ok": True, "eligible": eligible, "stage": stage}
        if action == "close":
            if not eligible:
                return {"ok": True, "closed": False, "stage": stage}
            await kc.move_to_status(lead_id, NOT_ACTUAL)
            await _set_status(cand.id, CandidateStatus.CLOSED)
            log.info("tg_silence.closed", candidate_id=cand.id, lead_id=lead_id)
            return {"ok": True, "closed": True, "stage": NOT_ACTUAL}
        if action == "reopen":
            if stage != NOT_ACTUAL or status != "closed":
                return {"ok": True, "reopened": False, "stage": stage}
            await kc.move_to_status(lead_id, IN_WORK)
            await _set_status(cand.id, CandidateStatus.MANAGER_REVIEW)
            log.info("tg_silence.reopened", candidate_id=cand.id, lead_id=lead_id)
            return {"ok": True, "reopened": True, "stage": IN_WORK}
        return {"ok": False, "error": f"unknown action: {action}"}
    finally:
        if crm is None:
            await kc.aclose()
