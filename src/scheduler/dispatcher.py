"""Call dispatcher — APScheduler cron runs every configured slot (e.g. 9, 11, 13, 15, 17, 19).

Picks candidates with status IN_CALL_QUEUE and attempts < CALL_MAX_ATTEMPTS,
respects MAX_CONCURRENT, dispatches via CallOrchestrator.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import and_, func, or_, select

from src.call.orchestrator import CallOrchestrator
from src.common.db import session_scope
from src.common.models import Call, Candidate, CandidateStatus
from src.common.settings import get_settings

log = logging.getLogger("recruiter.scheduler")

# One slot works the queue in batches; pause lets placed calls finish first.
SLOT_BATCH_PAUSE_SEC = 120
SLOT_MAX_BATCHES = 40  # safety cap (~120 candidates per slot)
HARD_CALL_CAP = int(os.environ.get("HARD_CALL_CAP", "4"))  # never call one person more than this, ever
REAL_CONTACT_SEC = 40   # a call this long counts as a real conversation
CALLING_WINDOW_END_HOUR = 20  # never start a new batch at or after 20:00 local
# Days a no-answer candidate may sit in "Недозвін" (call missed + Telegram fallback
# sent) before Eva gives up and moves them to "Не актуально" — so the column drains
# instead of piling up forever. Anyone who replied has already been moved by then.
UNREACHABLE_GIVEUP_DAYS = int(os.environ.get("UNREACHABLE_GIVEUP_DAYS", "3"))
# A job board poll must never outlive its own cadence.
POLL_TIMEOUT_SEC = int(os.environ.get("ROBOTAUA_POLL_TIMEOUT_SEC", "300"))


async def run_slot() -> None:
    from src.bot.admin import calls_paused
    if calls_paused():
        log.info("scheduler.paused — skipping slot")
        return
    s = get_settings()
    await _requeue_stuck_calls()
    # Honour recruiters' manual CRM moves before we dial: a candidate a recruiter took
    # over or dispositioned is now terminal here and drops out of the selection below.
    await sync_crm_stages()
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
                    # A due callback earns one more attempt beyond the normal limit —
                    # the candidate asked us to ring back, so honouring it is the point.
                    or_(
                        Candidate.call_attempts < s.call_max_attempts,
                        and_(
                            Candidate.callback_at.is_not(None),
                            Candidate.callback_at <= func.now(),
                        ),
                    ),
                    # Scheduled callback that is not due yet must wait its turn.
                    or_(
                        Candidate.callback_at.is_(None),
                        Candidate.callback_at <= func.now(),
                    ),
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

async def sync_crm_stages() -> None:
    """Pull each active Eva candidate's LIVE CRM stage and stop Eva if a recruiter has
    manually taken over or dispositioned the card (moved it off Eva's own working
    stages 1/2/31). Keeps candidate.status in step with hand edits in KeyCRM — which
    both the dispatcher and the Telegram gate read — so Eva goes quiet without needing a
    CRM→DB webhook. Runs periodically and at the start of every slot."""
    from src.common.crm import get_crm
    from src.common.keycrm_fields import crm_stage_stop_status

    kc = get_crm()
    changed = seen = 0
    try:
        async with session_scope() as session:
            rows = (await session.execute(
                select(Candidate).where(
                    Candidate.keycrm_lead_id.is_not(None),
                    Candidate.status.in_((
                        CandidateStatus.NEW_RESUME,
                        CandidateStatus.FILTERED,
                        CandidateStatus.IN_CALL_QUEUE,
                        CandidateStatus.UNREACHABLE,
                        CandidateStatus.CALL_DONE,
                    )),
                )
                # Bound the sweep so it never delays a slot: recruiters act on the
                # recently-touched cards, and anything skipped is caught next run.
                .order_by(Candidate.updated_at.desc())
                .limit(200)
            )).scalars().all()
            for cand in rows:
                seen += 1
                sid = await kc.get_card_status(cand.keycrm_lead_id)
                stop = crm_stage_stop_status(sid)
                if stop:
                    target = CandidateStatus(stop)
                    if cand.status != target:
                        log.info(
                            "scheduler.crm_sync_stop candidate=%d lead=%s stage=%s -> %s",
                            cand.id, cand.keycrm_lead_id, sid, stop,
                        )
                        cand.status = target
                        changed += 1
                await asyncio.sleep(0.3)  # KeyCRM rate limit (~60/min)
    finally:
        await kc.aclose()
    if seen:
        log.info("scheduler.crm_sync done changed=%d of=%d", changed, seen)


async def disposition_stale_unreachable() -> None:
    """Drain the 'Недозвін' column. A candidate who never answered the call AND never
    replied to the Telegram fallback within UNREACHABLE_GIVEUP_DAYS is given up: status
    → CLOSED and the CRM card → 'Не актуально' (32). Anyone who replied has already been
    moved off UNREACHABLE by the chat classifier, so only true no-shows are swept."""
    from src.common.crm import get_crm
    from src.common.keycrm_fields import STAGE_MAP

    cutoff = datetime.utcnow() - timedelta(days=UNREACHABLE_GIVEUP_DAYS)
    kc = get_crm()
    moved = 0
    try:
        async with session_scope() as session:
            rows = (await session.execute(
                select(Candidate).where(
                    Candidate.status == CandidateStatus.UNREACHABLE,
                    Candidate.updated_at < cutoff,
                )
            )).scalars().all()
            for cand in rows:
                cand.status = CandidateStatus.CLOSED
                if cand.keycrm_lead_id:
                    await kc.move_to_status(cand.keycrm_lead_id, STAGE_MAP.get("not_actual", 32))
                moved += 1
    finally:
        await kc.aclose()
    if moved:
        log.info("scheduler.unreachable_giveup moved=%d older_than=%dd", moved, UNREACHABLE_GIVEUP_DAYS)


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
                # A call that already carries a reason has been finalized — it is
                # not waiting for a webhook. Without this, calls the carrier
                # refused were re-finalized every ten minutes forever: they end up
                # FAILED by design, so reconcile kept picking them up, and each
                # pass counted another carrier fault. Three refused calls became
                # fifteen "faults" in one morning and tripped the line breaker.
                Call.ended_reason.is_(None),
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
                    # Vapi hands us the reason and this path used to drop it, which
                    # is why 434 rows carry an empty `ended_reason` — including every
                    # one of the 77 SIP 403s of 05–11.08. Without it nothing
                    # downstream can tell a refused call from an unanswered one.
                    ended_reason=d.get("endedReason"),
                )
                fixed += 1
            except Exception as e:
                log.warning("reconcile.failed", call=cid, error=str(e))
    if fixed:
        log.info("reconcile.done fixed=%d of=%d", fixed, len(rows))

async def send_backfill_outreach() -> None:
    """Daily: keep writing to the applicants the intake once walked past.

    Off unless OUTREACH_BACKFILL_AFTER names a start moment — without one the
    obvious query is "every sales candidate we never called", which is hundreds
    of people here. The userbot caps how many go out a day, so this runs until
    it says stop and picks the rest up tomorrow.
    """
    try:
        from src.integrations.tg_outreach import configured_start, run_once
        start = configured_start()
        if start is None:
            return
        stats = await run_once(created_after=start, send=True)
        log.info(
            "outreach.backfill pending=%d sent=%d skipped=%d stopped_on=%s",
            stats.pending, stats.sent, stats.skipped, stats.stopped_on or "-",
        )
    except Exception as e:  # noqa: BLE001
        log.error("outreach.backfill failed: %s", e)


async def check_workua_vacancy_liveness() -> None:
    """Twice a day: is each posting we recruit for still published?

    The poller cannot answer this. A deleted posting sends nothing, so
    `new=0 errors=0` reads identically to a quiet day — which is how both sales
    postings stayed dead for five days while the call queue starved.
    """
    try:
        from src.integrations.workua_liveness import run
        await run()
    except Exception as e:  # noqa: BLE001
        log.error("workua.liveness failed: %s", e)


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


async def poll_robotaua_responses() -> None:
    """robota.ua employer-cabinet inbound poller.

    Slower cadence than work.ua on purpose: the cabinet backend is behind
    Cloudflare and each new response costs an extra CV fetch, so a 10-minute
    beat keeps us far below anything that looks like scraping traffic.
    """
    from src.bot.admin import robotaua_paused
    if robotaua_paused():
        log.info("robotaua.paused — skipping poll")
        return
    if (os.environ.get("ROBOTAUA_POLL_ENABLED", "1") or "").strip().lower() in ("0", "false", "no", "off"):
        return
    try:
        from src.integrations.robotaua_sync import poll_responses
        # Hard ceiling: a poll that hangs would block every later run —
        # APScheduler allows one instance of a job at a time.
        stats = await asyncio.wait_for(poll_responses(), timeout=POLL_TIMEOUT_SEC)
        log.info(
            "robotaua.poll stats=new=%d accepted=%d duplicates=%d rejected=%d "
            "profile_rejected=%d no_phone=%d recovered=%d pending=%d errors=%d",
            stats.new, stats.accepted, stats.duplicates, stats.rejected,
            stats.profile_rejected, stats.no_phone, stats.recovered,
            stats.pending, stats.errors,
        )
    except Exception as e:
        log.exception("robotaua.poll_failed: %s", e)


async def poll_robotaua_chats() -> None:
    """robota.ua cabinet chat poller.

    The free path to candidates whose phone robota.ua hides: they wrote to us in
    the cabinet chat and often left a number in the text. Read-only — replying is
    gated separately inside the module.
    """
    from src.bot.admin import robotaua_paused
    if robotaua_paused():
        return
    if (os.environ.get("ROBOTAUA_CHAT_ENABLED", "1") or "").strip().lower() in ("0", "false", "no", "off"):
        return
    try:
        from src.integrations.robotaua_chat import poll_chats
        stats = await asyncio.wait_for(poll_chats(), timeout=POLL_TIMEOUT_SEC)
        log.info(
            "robotaua_chat.poll conversations=%d scanned=%d phones=%d replies=%d "
            "accepted=%d duplicates=%d rejected=%d errors=%d blocked=%s",
            stats.conversations, stats.scanned,
            stats.phones_from_email + stats.phones_from_text, stats.replies_sent,
            stats.accepted, stats.duplicates, stats.rejected, stats.errors, stats.blocked,
        )
    except Exception as e:
        log.exception("robotaua_chat.poll_failed: %s", e)


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
    # Write to the backfilled applicants a few at a time. Late morning on
    # purpose: a job message at 07:00 reads as spam.
    scheduler.add_job(
        send_backfill_outreach,
        trigger=CronTrigger(
            hour=os.environ.get("OUTREACH_BACKFILL_CRON_HOUR", "11"),
            minute=20,
            timezone=s.app_timezone,
        ),
        id="backfill_outreach",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    # Is anything we recruit for still actually published? Two public page hits
    # per posting per day — far under WORKUA_SCRAPE_DAILY_LIMIT, and the only
    # way a deletion can reach us at all.
    scheduler.add_job(
        check_workua_vacancy_liveness,
        trigger=CronTrigger(
            hour=os.environ.get("WORKUA_LIVENESS_CRON_HOUR", "9,17"),
            minute=11,
            timezone=s.app_timezone,
        ),
        id="workua_vacancy_liveness",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    # Poll robota.ua responses — offset from the work.ua poller so the two
    # inbound sources never hammer the intake in the same second.
    scheduler.add_job(
        poll_robotaua_responses,
        trigger=CronTrigger(
            minute=os.environ.get("ROBOTAUA_POLL_CRON_MINUTE", "3-59/10"),
            timezone=s.app_timezone,
        ),
        id="robotaua_poll",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=120,
    )
    # Cabinet chat: slower beat than the response poller — conversations move at
    # human speed and every extra request is Cloudflare exposure.
    scheduler.add_job(
        poll_robotaua_chats,
        trigger=CronTrigger(
            minute=os.environ.get("ROBOTAUA_CHAT_CRON_MINUTE", "8-59/15"),
            timezone=s.app_timezone,
        ),
        id="robotaua_chat_poll",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=120,
    )
    # Safety net: pull any call whose webhook never arrived.
    scheduler.add_job(
        reconcile_unfinalized,
        trigger=CronTrigger(minute="*/10", timezone=s.app_timezone),
        id="reconcile_calls",
        replace_existing=True,
    )
    # Mirror recruiters' manual CRM stage moves back into our DB so Eva stops calling
    # / messaging candidates they've taken over. Offset from reconcile to spread load.
    scheduler.add_job(
        sync_crm_stages,
        trigger=CronTrigger(minute="5-59/10", timezone=s.app_timezone),
        id="crm_stage_sync",
        replace_existing=True,
    )
    # Drain "Недозвін" daily before the first slot: give up on no-answer + no-reply
    # candidates so they don't pile up in the column forever.
    scheduler.add_job(
        disposition_stale_unreachable,
        trigger=CronTrigger(hour=8, minute=30, timezone=s.app_timezone),
        id="unreachable_giveup",
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
