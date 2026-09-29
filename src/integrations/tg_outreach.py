"""Message the applicants our intake walked past, instead of calling them.

The work.ua backfill of 2026-08-18 recovered 83 sales applicants nobody had ever
contacted. Calling them was ruled out: both sales postings were deleted from
work.ua on 13.08, so an outbound call would be about a job the candidate can no
longer look up. A message costs them nothing and lets whoever is still looking
answer in their own time.

The userbot sets the pace, not this module — a daily ceiling, an anti-spam check
per person, and a kill switch, all of which exist because the account was once
restricted for sending too much. Nothing here raises them. A run of eighty-odd
people therefore spans several days, and each run continues where the last
stopped; `candidates.outreach_sent_at` is what makes that safe to repeat.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import select

from src.common.activity import line_for, tg_failed_note, tg_sent_note
from src.common.crm import get_crm
from src.common.db import session_scope
from src.common.keycrm_fields import crm_stage_stop_status
from src.common.models import Candidate
from src.common.settings import get_settings

log = structlog.get_logger()

KIND = "applied_no_contact"

# A recruiter has already decided about these people. On 26.09 the walker wrote
# "we never reached you" to someone rejected as «Не ЦА» the day before.
_DECIDED = {"closed", "interview_scheduled"}

# Errors that will never succeed on a retry. Marking these done is not giving
# up — it is the difference between a queue that drains and one that jams on the
# same person every run.
_PERMANENT = ("не в Telegram", "приватність", "Вже писали", "privacy", "PREMIUM")


@dataclass
class OutreachStats:
    pending: int = 0
    sent: int = 0
    skipped: int = 0
    stopped_on: str = ""


async def pending_candidates(
    vacancy: str, created_after: datetime, limit: int
) -> list[tuple[int, str, str]]:
    """(id, name, phone) for people still owed a message, oldest first."""
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Candidate.id, Candidate.full_name, Candidate.phone_e164)
                .where(
                    Candidate.vacancy_key == vacancy,
                    Candidate.created_at >= created_after,
                    Candidate.outreach_sent_at.is_(None),
                    Candidate.call_attempts == 0,
                    Candidate.phone_e164.isnot(None),
                )
                .order_by(Candidate.created_at)
                .limit(limit)
            )
        ).all()
    return [(r[0], r[1] or "", r[2]) for r in rows]


async def _card_facts(ids: list[int]) -> dict[int, tuple]:
    """candidate id -> (status, callback_at, keycrm_lead_id)."""
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Candidate.id, Candidate.status, Candidate.callback_at,
                       Candidate.keycrm_lead_id)
                .where(Candidate.id.in_(ids))
            )
        ).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def skip_reason(crm, *, status, callback_at, lead_id: int | None) -> str | None:
    """Why this person must not get the message, or None to send it.

    Asks KeyCRM for the live stage: our own status lags behind a recruiter's moves
    (a card rejected in the CRM stays `manager_review` here). Raises when KeyCRM
    cannot answer — nobody is written to on a guess.
    """
    status = str(getattr(status, "value", status) or "")
    if status in _DECIDED:
        return f"status:{status}"
    if not lead_id:
        return None
    stage = await crm.live_card_status(int(lead_id))
    if stage is None:
        return "card_deleted"
    if crm_stage_stop_status(stage, status, callback_at) is not None:
        return f"crm_stage:{stage}"
    return None


async def _mark_sent(candidate_id: int) -> None:
    async with session_scope() as s:
        row = (
            await s.execute(select(Candidate).where(Candidate.id == candidate_id))
        ).scalar_one_or_none()
        if row:
            row.outreach_sent_at = datetime.now(UTC)


async def _note_tg(kc, cid: int, lead_id: int | None, note: str) -> None:
    """Remember what happened in Telegram and show it on the lead card. Bookkeeping:
    a failure here must never stop the walk."""
    try:
        async with session_scope() as s:
            row = (
                await s.execute(select(Candidate).where(Candidate.id == cid))
            ).scalar_one_or_none()
            if row:
                row.tg_note = note
        if lead_id:
            await kc.set_activity_line(int(lead_id), await line_for(cid))
    except Exception as e:  # noqa: BLE001 — the message went out, the card can wait
        log.warning("tg_outreach.activity_failed", candidate_id=cid, error=str(e))


async def run_once(
    *,
    vacancy: str = "sales",
    created_after: datetime,
    limit: int = 100,
    send: bool = False,
    on_event=None,
    crm=None,
) -> OutreachStats:
    """One pass. Stops the moment the userbot says it has had enough for today."""
    people = await pending_candidates(vacancy, created_after, limit)
    stats = OutreachStats(pending=len(people))
    if not people or not send:
        return stats

    facts = await _card_facts([p[0] for p in people])
    kc = crm or get_crm()
    url = f"{get_settings().tguserbot_url}/send_outreach"
    try:
        await _walk(people, facts=facts, kc=kc, url=url, stats=stats, on_event=on_event)
    finally:
        if crm is None:
            await kc.aclose()

    log.info(
        "tg_outreach.run",
        vacancy=vacancy,
        pending=stats.pending,
        sent=stats.sent,
        skipped=stats.skipped,
        stopped_on=stats.stopped_on,
    )
    return stats


async def _walk(people, *, facts, kc, url, stats: OutreachStats, on_event) -> None:
    async with httpx.AsyncClient(timeout=90) as http:
        for cid, name, phone in people:
            status, callback_at, lead_id = facts.get(cid, (None, None, None))
            try:
                why = await skip_reason(kc, status=status, callback_at=callback_at,
                                        lead_id=lead_id)
            except Exception as e:  # noqa: BLE001 — KeyCRM unreachable: keep them pending
                stats.stopped_on = f"keycrm: {e}"[:120]
                break
            if why:
                await _mark_sent(cid)
                stats.skipped += 1
                log.info("tg_outreach.skip", candidate_id=cid, reason=why)
                if on_event:
                    on_event("skipped", name, why)
                continue

            try:
                r = await http.post(url, json={"phone": phone, "name": name, "kind": KIND})
                data = r.json()
            except Exception as e:  # noqa: BLE001
                stats.stopped_on = f"userbot unreachable: {e}"
                break

            if data.get("ok"):
                await _mark_sent(cid)
                await _note_tg(kc, cid, lead_id, tg_sent_note(datetime.now(UTC)))
                stats.sent += 1
                if on_event:
                    on_event("sent", name, "")
                continue

            err = str(data.get("error") or "")[:120]
            if any(token in err for token in _PERMANENT):
                await _mark_sent(cid)
                if tg_failed_note(err):
                    await _note_tg(kc, cid, lead_id, tg_failed_note(err))
                stats.skipped += 1
                if on_event:
                    on_event("skipped", name, err)
                continue

            # Daily ceiling, flood wait, paused switch — keep the row pending.
            stats.stopped_on = err
            if on_event:
                on_event("stopped", name, err)
            break


def configured_start() -> datetime | None:
    """`OUTREACH_BACKFILL_AFTER` — the scheduled walker does nothing without it.

    Deliberately opt-in and deliberately a timestamp rather than a flag: the
    natural query without one is "every sales candidate we never called", which
    on this database is hundreds of people, including everyone Eva already spoke
    to months ago.
    """
    raw = (os.getenv("OUTREACH_BACKFILL_AFTER") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("tg_outreach.bad_start", value=raw)
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
