"""Telegram chat outcome -> CRM.

The userbot classifies each dialog and, when it reaches a verdict, calls this
handler. It mirrors what the call orchestrator does after a phone screening:
a qualified candidate lands in "Відібрано", a rejected one in "Не підходить нам",
the conversation summary/transcript go on the card, and the AI manager is set so
recruiters can tell it apart. Runs on the API service so all CRM logic stays in
one place; the userbot stays a thin messaging layer.
"""
from __future__ import annotations

import structlog
from sqlalchemy import select

from src.common import sources
from src.common.db import session_scope
from src.common.keycrm import KeyCRMClient
from src.common.keycrm_fields import STAGE_MAP
from src.common.models import Candidate, CandidateStatus
from src.common.phone import normalize_phone
from src.common.settings import get_settings

log = structlog.get_logger()

# Verdict from the chat classifier -> (our status, CRM stage key)
_VERDICT_MAP = {
    "qualified": (CandidateStatus.MANAGER_REVIEW, "manager_review"),
    "not_fit": (CandidateStatus.CLOSED, "closed"),
}


def _tg_phone(phone: str | None, peer_id: str) -> tuple[str, bool]:
    """Return a phone key for the candidate and whether it is a real number.

    Telegram often hides the number, but phone_e164 is unique+NOT NULL. When we
    have a real number we use it (so a chat and a call for the same person
    dedupe into one candidate). Otherwise we mint a stable synthetic key from the
    Telegram id — unique, fits String(20), never collides with a real +380 number.
    """
    if phone:
        try:
            norm = normalize_phone(phone)
            if norm:
                return norm, True
        except Exception:
            pass
    return f"tg{peer_id}"[:20], False


async def handle_tg_outcome(
    *,
    peer_id: str,
    name: str,
    username: str | None,
    phone: str | None,
    verdict: str,
    region: str | None,
    age: int | None,
    summary: str,
    transcript: str,
) -> dict:
    """Create/update a CRM card for a Telegram candidate who reached a verdict."""
    mapping = _VERDICT_MAP.get(verdict)
    if not mapping:
        return {"ok": False, "error": f"non-terminal verdict: {verdict}"}
    status, stage_key = mapping
    stage_id = STAGE_MAP.get(stage_key)
    s = get_settings()

    phone_key, real_phone = _tg_phone(phone, peer_id)
    handle = f"@{username}" if username else f"tg:{peer_id}"

    # --- find or create the local candidate ---
    async with session_scope() as sess:
        cand = (await sess.execute(
            select(Candidate).where(Candidate.phone_e164 == phone_key)
        )).scalar_one_or_none()
        if cand is None:
            cand = Candidate(
                full_name=name or handle,
                phone_e164=phone_key,
                region=(region or None),
                source="telegram",
                status=status,
            )
            sess.add(cand)
            await sess.flush()
        else:
            cand.status = status
            if region and not cand.region:
                cand.region = region
        cand_id = cand.id
        lead_id = cand.keycrm_lead_id

    # --- CRM: create the card if missing, then fill it and move the stage ---
    kc = KeyCRMClient()
    vacancy_name = s.default_vacancy_title
    note = (
        f"Джерело: Telegram ({handle})"
        + (f" | тел.: {phone_key}" if real_phone else " | номер прихований")
        + f"\nВердикт: {'КВАЛІФІКОВАНИЙ' if verdict == 'qualified' else 'не підходить'}"
        + (f" | вік {age}" if age else "")
        + (f" | {region}" if region else "")
        + f"\n{(summary or '')[:1500]}"
    )
    try:
        if not lead_id:
            created = await kc.create_lead(
                title=name or handle,
                full_name=name or handle,
                phone=phone_key,
                vacancy_name=vacancy_name,
                manager_comment=note,
            )
            lead_id = int(created.get("id") or 0) or None
            async with session_scope() as sess:
                c = await sess.get(Candidate, cand_id)
                if c and lead_id:
                    c.keycrm_lead_id = lead_id
        if lead_id:
            await kc.write_call_results(
                lead_id,
                summary=summary,
                transcript=transcript,
                region=region,
            )
            if s.keycrm_ai_manager_id:
                await kc.assign_manager(lead_id, s.keycrm_ai_manager_id)
            if stage_id is not None:
                await kc.move_to_status(lead_id, stage_id)
    except Exception as e:
        log.warning("tg_outcome.crm_failed", error=str(e), candidate_id=cand_id)
        return {"ok": False, "error": f"crm: {e}", "candidate_id": cand_id}

    log.info(
        "tg_outcome.done",
        candidate_id=cand_id,
        lead_id=lead_id,
        verdict=verdict,
        real_phone=real_phone,
    )
    return {"ok": True, "candidate_id": cand_id, "lead_id": lead_id, "verdict": verdict}
