"""Call dispatcher — APScheduler cron runs every configured slot (e.g. 9, 11, 13, 15, 17, 19).

Picks candidates with status IN_CALL_QUEUE and attempts < CALL_MAX_ATTEMPTS,
respects MAX_CONCURRENT, dispatches via CallOrchestrator.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from src.call.orchestrator import CallOrchestrator
from src.common.db import session_scope
from src.common.models import Call, Candidate, CandidateStatus
from src.common.settings import get_settings

log = logging.getLogger("recruiter.scheduler")

# One slot works the queue in batches; pause lets placed calls finish first.
SLOT_BATCH_PAUSE_SEC = 120
SLOT_MAX_BATCHES = 40  # safety cap (~120 candidates per slot)
HARD_CALL_CAP = 4       # never call one person more than this, ever
REAL_CONTACT_SEC = 40   # a call this long counts as a real conversation
CALLING_WINDOW_END_HOUR = 20  # never start a new batch at or after 20:00 local


async def run_slot() -> None:
    from src.bot.admin import calls_paused
    if calls_paused():
        log.info("scheduler.paused — skipping slot")
        return
    s = get_settings()
    await _requeue_stuck_calls()
    orchestrator = CallOrchestrator()

    # A slot is a calling SESSION: keep pulling batches until the queue is empty.
    # Without this a slot dialed only MAX_CONCURRENT people and a large queue
    # would take days to work through.
    total, batches = 0, 0
    while batches < SLOT_MAX_BATCHES:
        async with session_scope() as session:
            q = await session.execute(
                select(Candidate)
                .where(
                    Candidate.status.in_(
                        (
                            CandidateStatus.NEW_RESUME,
                            CandidateStatus.IN_CALL_QUEUE,
                        )
                    ),
                    Candidate.call_attempts < s.call_max_attempts,
                    # hard, reset-proof guards computed from the calls table
                    (
                        select(func.count(Call.id))
                        .where(Call.candidate_id == Candidate.id)
                        .scalar_subquery()
                    ) < HARD_CALL_CAP,
                    (
                        select(func.count(Call.id))
                        .where(
                            Call.candidate_id == Candidate.id,
                            Call.duration_sec >= REAL_CONTACT_SEC,
                        )
                        .scalar_subquery()
                    ) == 0,
                )
                .order_by(Candidate.match_score.desc().nulls_last(), Candidate.created_at)
                .limit(s.call_max_concurrent)
            )
            batch = q.scalars().all()
            if not batch:
                break
            ids = [c.id for c in batch]

        batches += 1
        total += len(ids)
        log.info("scheduler.batch start=%d size=%d total=%d", batches, len(ids), total)
        await asyncio.gather(*(orchestrator.dispatch_for_candidate(cid) for cid in ids))

        # Let the placed calls run before dialing the next batch, otherwise
        # concurrency grows unbounded across the session.
        if calls_paused():
            log.info("scheduler.paused_mid_session — stopping after batch %d", batches)
            break
        if _past_calling_window():
            log.info("scheduler.window_closed — stopping after batch %d", batches)
            break
        await asyncio.sleep(SLOT_BATCH_PAUSE_SEC)

    if total == 0:
        log.info("scheduler.empty_slot")
    else:
        log.info("scheduler.slot_done batches=%d dialed=%d", batches, total)




def _past_calling_window() -> bool:
    """True once we are outside the hours it is acceptable to call people."""
    from zoneinfo import ZoneInfo

    s = get_settings()
    now = datetime.now(ZoneInfo(s.app_timezone))
    return now.hour >= CALLING_WINDOW_END_HOUR

async def _requeue_stuck_calls(max_age_min: int = 15) -> None:
    """Return candidates stranded in CALLING back to the queue.

    A crash between "status = CALLING" and the Vapi request leaves them stuck
    forever, so they silently drop out of the campaign.
    """
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(minutes=max_age_min)
    async with session_scope() as session:
        rows = (await session.execute(
            select(Candidate).where(
                Candidate.status == CandidateStatus.CALLING,
                Candidate.updated_at < cutoff,
            )
        )).scalars().all()
        for cand in rows:
            cand.status = CandidateStatus.IN_CALL_QUEUE
            cand.call_attempts = max(0, cand.call_attempts - 1)
        if rows:
            log.info("scheduler.requeued_stuck count=%d", len(rows))



async def _catch_up_missed_slot() -> None:
    """Run a session if a slot was missed while the service was down/redeploying.

    Only inside working hours, and only if a slot time has already passed today —
    so a restart never turns into an out-of-hours call.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    s = get_settings()
    now = datetime.now(ZoneInfo(s.app_timezone))
    slots = []
    for slot in s.call_slot_times:
        h, m = slot.split(":")
        slots.append(now.replace(hour=int(h), minute=int(m), second=0, microsecond=0))

    passed = [t for t in slots if t <= now]
    if not passed:
        log.info("scheduler.catchup_skipped reason=before_first_slot")
        return

    # Do not fire hours after the last slot (e.g. a restart at 23:00).
    if (now - passed[-1]).total_seconds() > 3600:
        log.info("scheduler.catchup_skipped reason=too_late_after_slot")
        return

    log.info("scheduler.catchup_start missed_slot=%s", passed[-1].strftime("%H:%M"))
    await run_slot()


async def reconcile_unfinalized() -> None:
    """Finalize calls whose end-of-call webhook never landed (safety net)."""
    import os
    from datetime import timedelta
    import httpx
    from src.common.models import Call, CallStatus
    from src.call.orchestrator import CallOrchestrator

    now = datetime.utcnow()
    lo = now - timedelta(hours=3)          # only recent calls
    hi = now - timedelta(minutes=3)        # give the webhook a chance first
    async with session_scope() as sess:
        rows = (await sess.execute(
            select(Call.vapi_call_id).where(
                Call.status == CallStatus.FAILED,
                Call.vapi_call_id != "",
                Call.started_at >= lo,
                Call.started_at <= hi,
            )
        )).scalars().all()
    if not rows:
        return

    orch = CallOrchestrator()
    fixed = 0
    async with httpx.AsyncClient(
        base_url="https://api.vapi.ai",
        headers={"Authorization": f"Bearer {os.environ['VAPI_API_KEY']}"},
        timeout=30,
    ) as c:
        for cid in rows:
            try:
                r = await c.get(f"/call/{cid}")
                if r.status_code != 200:
                    continue
                d = r.json()
                if d.get("status") != "ended":
                    continue
                dur = 0.0
                if d.get("startedAt") and d.get("endedAt"):
                    f = lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))
                    dur = (f(d["endedAt"]) - f(d["startedAt"])).total_seconds()
                await orch.process_end_of_call(
                    vapi_call_id=cid,
                    transcript=d.get("transcript") or "",
                    duration_sec=dur,
                    recording_url=(d.get("recordingUrl")
                                   or (d.get("artifact") or {}).get("recordingUrl")),
                )
                fixed += 1
            except Exception as e:
                log.warning("reconcile.failed", call=cid, error=str(e))
    if fixed:
        log.info("reconcile.done fixed=%d of=%d", fixed, len(rows))

async def poll_workua_responses() -> None:
    """work.ua API inbound poller — runs every 5 min."""
    from src.bot.admin import workua_paused
    if workua_paused():
        log.info("workua.paused — skipping poll")
        return
    try:
        from src.integrations.workua_sync import poll_responses
        stats = await poll_responses()
        log.info(
            "workua.poll stats=new=%d accepted=%d duplicates=%d rejected=%d profile_rejected=%d errors=%d",
            stats.new_responses, stats.accepted, stats.duplicates,
            stats.rejected, stats.profile_rejected, stats.errors,
        )
    except Exception as e:
        log.exception("workua.poll_failed: %s", e)


def build_scheduler() -> AsyncIOScheduler:
    s = get_settings()
    scheduler = AsyncIOScheduler(timezone=s.app_timezone)
    for slot in s.call_slot_times:
        hour, minute = slot.split(":")
        scheduler.add_job(
            run_slot,
            trigger=CronTrigger(hour=int(hour), minute=int(minute), timezone=s.app_timezone),
            id=f"call_slot_{hour}_{minute}",
            replace_existing=True,
        )
    # Poll work.ua every 5 min
    scheduler.add_job(
        poll_workua_responses,
        trigger=CronTrigger(minute="*/5", timezone=s.app_timezone),
        id="workua_poll",
        replace_existing=True,
    )
    # Safety net: pull any call whose webhook never arrived.
    scheduler.add_job(
        reconcile_unfinalized,
        trigger=CronTrigger(minute="*/10", timezone=s.app_timezone),
        id="reconcile_calls",
        replace_existing=True,
    )
    return scheduler


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    scheduler = build_scheduler()
    scheduler.start()
    # A deploy that lands after a slot would otherwise skip it entirely.
    asyncio.create_task(_catch_up_missed_slot())
    log.info("scheduler.started")
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(_main())
