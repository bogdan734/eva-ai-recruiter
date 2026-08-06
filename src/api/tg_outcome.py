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
from src.common.keycrm_fields import STAGE_MAP, crm_stage_stop_status
from src.common.models import Candidate, CandidateStatus
from src.common.phone import normalize_phone
from src.common.settings import get_settings

log = structlog.get_logger()

# Verdict from the chat classifier -> (our status, CRM stage key).
# qualified → В роботі (3); not_fit defaults to Не актуально (32). A more specific
# reason ("misbehaved" → Не підходить 33, "not_target" → Не ЦА 34) overrides below.
_VERDICT_MAP = {
    "qualified": (CandidateStatus.MANAGER_REVIEW, "manager_review"),
    "not_fit": (CandidateStatus.CLOSED, "not_actual"),
}
_REASON_STAGE = {
    "misbehaved": "we_rejected",   # 33 Не підходить нам
    "not_target": "not_target",    # 34 Не ЦА
    "not_interested": "not_actual",  # 32 Не актуально
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
    reason: str = "none",
) -> dict:
    """Create/update a CRM card for a Telegram candidate who reached a verdict."""
    mapping = _VERDICT_MAP.get(verdict)
    if not mapping:
        return {"ok": False, "error": f"non-terminal verdict: {verdict}"}
    status, stage_key = mapping
    # A rejection reason refines the "not_fit" bucket into 32/33/34.
    if verdict == "not_fit" and reason in _REASON_STAGE:
        stage_key = _REASON_STAGE[reason]
    stage_id = STAGE_MAP.get(stage_key)
    s = get_settings()

    phone_key, real_phone = _tg_phone(phone, peer_id)
    handle = f"@{username}" if username else f"tg:{peer_id}"

    # --- find or create the local candidate ---
    async with session_scope() as sess:
        cand = (await sess.execute(
            select(Candidate).where(Candidate.phone_e164 == phone_key)
        )).scalar_one_or_none()
        # Hidden Telegram number: fall back to the name so a chat verdict lands on the
        # card we already called, instead of minting a tg<peer> duplicate. Same rule
        # handle_tg_progress uses — they must resolve candidates identically.
        if cand is None and not real_phone and name:
            cand = (await sess.execute(
                select(Candidate).where(
                    Candidate.full_name == name,
                    Candidate.phone_e164.like("+%"),
                ).limit(1)
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
                age=age,
                city=region,
            )
            if s.keycrm_ai_manager_id:
                await kc.assign_manager(lead_id, s.keycrm_ai_manager_id)
            # NEVER overwrite a recruiter's own decision. If the card already sits in a
            # disposition/handoff stage (В роботі, Не ЦА, Не підходить, Запросили…), a
            # human has judged this person — a later chat verdict must only add the
            # transcript, not drag the card back into Eva's funnel.
            if stage_id is not None:
                live_stage = await kc.get_card_status(lead_id)
                if crm_stage_stop_status(live_stage) is not None:
                    log.info(
                        "tg_outcome.stage_kept",
                        lead_id=lead_id, live_stage=live_stage, wanted=stage_id,
                    )
                else:
                    await kc.move_to_status(lead_id, stage_id)
            # Save as buyer (green 'client' check), link the card, and note the
            # outcome — only for a real phone (shared, dedupable contact). Hidden-number
            # TG leads are already linked to a name-only buyer at create_lead time.
            if real_phone:
                lbl = (
                    "✅ кваліфікований → В роботі" if verdict == "qualified"
                    else "🚫 не ЦА" if reason == "not_target"
                    else "❌ не підходить" if reason == "misbehaved"
                    else "⚪ не актуально"
                )
                buyer_id = await kc.ensure_buyer(full_name=name or handle, phone=phone_key)
                if buyer_id:
                    if lead_id:
                        await kc.link_card_to_buyer(lead_id, buyer_id)
                    await kc.write_buyer_call_status(buyer_id, f"Telegram · {lbl}")
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


async def handle_tg_progress(
    *,
    peer_id: str,
    name: str,
    username: str | None,
    phone: str | None,
    transcript: str,
) -> dict:
    """Keep an in-progress Telegram chat visible in CRM before it reaches a verdict.

    If the candidate already has a card, refresh its transcript live — so a recruiter
    can read the conversation as it happens, and it survives the candidate deleting the
    chat on their side. No card yet → no-op (we don't create a card per message, keeping
    the funnel clean); the full transcript still lands when the chat reaches a verdict,
    and our local store keeps every message regardless."""
    phone_key, real = _tg_phone(phone, peer_id)
    handle = f"@{username}" if username else f"tg:{peer_id}"
    s = get_settings()
    # Resolve the candidate by phone/surrogate first, then by NAME — a hidden-number TG
    # reply from someone we already called by phone must land on THEIR card, not a new
    # duplicate. lead_phone is the real number when we match a phone candidate.
    async with session_scope() as sess:
        cand = (await sess.execute(
            select(Candidate).where(Candidate.phone_e164 == phone_key)
        )).scalar_one_or_none()
        if cand is None and name:
            cand = (await sess.execute(
                select(Candidate).where(
                    Candidate.full_name == name,
                    Candidate.phone_e164.like("+%"),
                ).limit(1)
            )).scalar_one_or_none()
        cand_id = cand.id if cand else None
        lead_id = cand.keycrm_lead_id if cand else None
        lead_phone = cand.phone_e164 if cand else phone_key
    kc = KeyCRMClient()
    try:
        # Already carded → refresh the live transcript on that card (LD_1006). This is
        # also the merge point: a name-matched call card gets the Telegram dialog too.
        if lead_id:
            await kc.write_call_results(lead_id, transcript=transcript[:6000])
            return {"ok": True, "lead_id": lead_id}
        # Not carded. Anchor a card so the dialog is visible in CRM 1-to-1 (and survives
        # deletion), but only once the chat is REAL — the candidate answered at least
        # twice — so a bare greeting never spawns a card.
        if transcript.count("[Кандидат]") < 2:
            return {"ok": True, "skipped": "dialog_too_short"}
        if cand_id is None:
            async with session_scope() as sess:
                cand = Candidate(
                    full_name=name or handle,
                    phone_e164=phone_key,
                    source="telegram",
                    status=CandidateStatus.CALL_DONE,  # engaged in chat, not in the call queue
                )
                sess.add(cand)
                await sess.flush()
                cand_id = cand.id
            lead_phone = phone_key
        created = await kc.create_lead(
            title=name or handle,
            full_name=name or handle,
            phone=lead_phone,
            vacancy_name=s.default_vacancy_title,
            manager_comment="Джерело: Telegram — активна переписка",
            status_id=STAGE_MAP.get("call_done", 2),   # 2 Відібрано (pool)
        )
        lead_id = int(created.get("id") or 0) or None
        if lead_id:
            async with session_scope() as sess:
                c = await sess.get(Candidate, cand_id)
                if c:
                    c.keycrm_lead_id = lead_id
            await kc.write_call_results(lead_id, transcript=transcript[:6000])
            if s.keycrm_ai_manager_id:
                await kc.assign_manager(lead_id, s.keycrm_ai_manager_id)
            # KeyCRM ignores the create-time status_id when the card is created ON a
            # buyer (contact.client_id) — it drops into "Новий". Move it explicitly
            # into the "Відібрано" pool, the same PUT-based pattern the call
            # orchestrator and handle_tg_outcome already rely on.
            await kc.move_to_status(lead_id, STAGE_MAP.get("call_done", 2))
        return {"ok": True, "lead_id": lead_id, "created": True}
    except Exception as e:
        log.warning("tg_progress.crm_failed", error=str(e), lead_id=lead_id)
        return {"ok": False, "error": str(e)}
    finally:
        await kc.aclose()
