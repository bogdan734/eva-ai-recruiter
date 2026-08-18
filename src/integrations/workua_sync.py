"""work.ua → KeyCRM sync workers.

Two streams:
  1. `poll_responses()` — every 5 min, fetch new responses (FREE), feed into
     InboundRouter. This is the cheap inbound that mirrors what Apix-Drive used
     to do but via official API.
  2. `search_and_qualify(query, vacancy_id)` — proactive resume DB search.
     EXPENSIVE (paid credits per opened contact). Run only at cron slots
     against active vacancies, with profile pre-filter.

State (last processed response id, etc.) lives in `sync_state` table —
new in this migration.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from src.api.inbound_router import IngestPayload, InboundRouter
from src.common import vacancies
from src.common.state import state_dir
from src.integrations.workua_api import (
    WorkUaApiError,
    WorkUaAuthError,
    WorkUaClient,
    WorkUaRateLimitError,
    parse_resume,
    parse_response,
)
from src.match.profile_filter import FilterResult
from src.match.profile_filter import evaluate as profile_evaluate

# Stand-in verdict for vacancies whose candidates are not screened by the sales
# portrait — the recruiter reads the card instead.
_ACCEPTED = FilterResult(accepted=True, reason="intake_only_vacancy")
from src.match.scorer import MatchScorer

log = structlog.get_logger()

CURSOR_NAME = "workua_cursor.json"


def _cursor_path() -> Path:
    """Cursor lives on the mounted state volume, not inside the image.

    It used to be a hardcoded `.cache/workua_cursor.json`, which resolves to
    `/app/.cache/` in the container — a path baked into the image and therefore
    wiped by every `docker compose build`. Each deploy silently reset
    `responses_last_id` and the poller re-walked its backfill window. Same
    directory the robota.ua cursors already use.
    """
    return state_dir() / CURSOR_NAME


@dataclass
class PollStats:
    new_responses: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    profile_rejected: int = 0
    errors: int = 0
    last_id: int | None = None


def _load_cursor() -> dict[str, Any]:
    path = _cursor_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cursor(state: dict[str, Any]) -> None:
    path = _cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _allowed_vacancy_ids() -> set[int]:
    """Which work.ua job ids we pull Відгуки for.

    The vacancy registry is the base — every job id we recruit for, including
    the intake-only ones. WORKUA_ALLOWED_VACANCY_IDS stays supported and is
    added on top, so an id can still be switched on from the environment
    without a code change.
    """
    import os
    out: set[int] = set(vacancies.workua_ids())
    raw = (os.getenv("WORKUA_ALLOWED_VACANCY_IDS") or "").strip()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except ValueError:
            log.warning("workua.bad_vacancy_id_in_env", token=tok)
    return out


SKIPPED_LEDGER_KEY = "skipped_jobs"


def _record_skipped(cursor: dict[str, Any], *, job_id: int, response_id: int) -> None:
    """Remember a response we could not route, so it can be replayed later.

    The cursor moves on regardless — the feed is one stream shared by every
    vacancy, and holding it back for a single unmapped posting would stall all
    the others. Remembering the earliest id we walked past is what turns the
    skip from a permanent loss into a recoverable one.
    """
    ledger = cursor.setdefault(SKIPPED_LEDGER_KEY, {})
    # last_id is exclusive: to fetch response N again we have to ask from N-1.
    entry = ledger.setdefault(str(job_id), {"resume_from": response_id - 1, "count": 0})
    entry["resume_from"] = min(int(entry["resume_from"]), response_id - 1)
    entry["count"] = int(entry.get("count", 0)) + 1


def _due_catch_ups(cursor: dict[str, Any]) -> list[tuple[int, int]]:
    """Skipped jobs somebody has since mapped in the vacancy registry.

    Everything else stays on the ledger: the account also carries postings we
    genuinely do not recruit for, and those must never be replayed.
    """
    out: list[tuple[int, int]] = []
    for raw_id, entry in (cursor.get(SKIPPED_LEDGER_KEY) or {}).items():
        try:
            job_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if vacancies.for_workua(job_id) is not None:
            out.append((job_id, int(entry.get("resume_from", 0))))
    return sorted(out)


async def _ingest_response(resp: Any, *, router: InboundRouter, stats: PollStats) -> None:
    """Route one parsed response into its funnel.

    Shared by the live poll and the catch-up replay on purpose: a recovered
    response has to pass exactly the same gates as a fresh one, or the two paths
    drift and only one of them gets the next screening fix.
    """
    if not resp.phone:
        stats.rejected += 1
        return

    route = vacancies.for_workua(resp.job_id) or vacancies.DEFAULT
    full_name = resp.fio or "Кандидат work.ua"
    # Quick profile filter — region/age via from_type is not available here yet,
    # but birth_date is.
    birth_year = None
    if resp.birth_date and len(resp.birth_date) >= 4:
        try:
            birth_year = int(resp.birth_date[:4])
        except ValueError:
            birth_year = None

    # The portrait below is the sales one. An intake-only vacancy has its own
    # requirements and a human reads the card, so it goes straight through.
    profile = _ACCEPTED if not route.screen_enabled else profile_evaluate(
        full_name=full_name,
        region=None,  # not in response payload; AI will ask on call
        desired_position=resp.text or resp.cover,
        last_position=None,
        resume_text=(resp.text or "") + " " + (resp.cover or ""),
        birth_year=birth_year,
    )
    if not profile.accepted:
        stats.profile_rejected += 1
        log.info("workua.profile_rejected", id=resp.id, reason=profile.reason)
        return

    # Employer-cabinet link to this applicant's resume (FREE — no paid contact
    # opening). Format confirmed 2026-07-22: /employer/my/applicants/{candidate_id}/.
    # Never emit a dangling "?jobId=" — a response that came back without a
    # job_id used to produce exactly that, and the link then opens nothing.
    # The applicant page works on its own; the query only preselects the job.
    applicant_url = None
    if resp.candidate_id:
        applicant_url = (
            f"https://www.work.ua/employer/my/applicants/{resp.candidate_id}/"
        )
        if resp.job_id:
            applicant_url += f"?jobId={resp.job_id}"
    result = await router.ingest(
        IngestPayload(
            full_name=full_name,
            phone_raw=resp.phone,
            email=resp.email,
            region_raw=None,
            desired_position=None,
            work_ua_url=applicant_url,
            workua_response_id=str(resp.id),
            resume_text=((resp.text or "") + (("\n\n" + resp.cover) if resp.cover else "")).strip() or None,
            source=f"workua_response_{resp.from_type}",
            vacancy_id=vacancies.LOCAL_FK,  # local FK; work.ua job_id lives in raw payload
            vacancy_key=route.key,
        )
    )
    if not result.accepted:
        stats.rejected += 1
    elif result.duplicate:
        stats.duplicates += 1
    else:
        stats.accepted += 1


async def catch_up_skipped(
    *,
    client: WorkUaClient,
    router: InboundRouter,
    cursor: dict[str, Any],
    page_size: int = 50,
    max_pages: int = 40,
) -> int:
    """Replay the responses skipped while their posting was still unmapped.

    Runs by itself at the top of every poll. A republished vacancy always
    arrives as an unknown id first, so the applicants behind it are skipped
    before anyone can know they mattered; once the id is added in the panel this
    pulls them back with no cursor surgery and nobody having to spot a warning
    line. Bounded by `max_pages` so one very old ledger entry cannot hold up the
    live poll — whatever it does not reach this run it reaches on the next.

    Idempotent: the router dedupes by phone, so a replay that overlaps work
    already done costs lookups, not duplicate cards.
    """
    due = _due_catch_ups(cursor)
    if not due:
        return 0

    stop_at = int(cursor.get("responses_last_id") or 0)
    replayed = 0
    for job_id, resume_from in due:
        stats = PollStats()
        # Per job, not cumulative: this line is the receipt somebody reads to
        # decide whether a panel edit worked, so it has to say what THIS posting
        # gave back.
        for_this_job = 0
        last = resume_from
        pages = 0
        while pages < max_pages:
            page = await client.list_responses(
                limit=page_size, last_id=last, sort=1, from_types=["send", "phonecall"]
            )
            items = page.get("items") or []
            if not items:
                break
            pages += 1
            for raw in items:
                try:
                    resp = parse_response(raw)
                except Exception as e:
                    log.warning(
                        "workua.catch_up.parse_failed", error=str(e), raw_id=raw.get("id")
                    )
                    continue
                last = max(last, resp.id)
                if resp.job_id != job_id:
                    continue
                for_this_job += 1
                await _ingest_response(resp, router=router, stats=stats)
            if last >= stop_at:
                break
        (cursor.get(SKIPPED_LEDGER_KEY) or {}).pop(str(job_id), None)
        # WARNING because it is the receipt for a silent loss being undone —
        # this line is how you find out the panel edit actually took effect.
        replayed += for_this_job
        log.warning(
            "workua.catch_up.done",
            job_id=job_id,
            replayed=for_this_job,
            accepted=stats.accepted,
            duplicates=stats.duplicates,
            rejected=stats.rejected,
            profile_rejected=stats.profile_rejected,
        )
    return replayed


async def poll_responses(
    *,
    client: WorkUaClient | None = None,
    router: InboundRouter | None = None,
    include_phonecalls: bool = True,
    page_size: int = 50,
) -> PollStats:
    """Pull new responses since last_id and feed them through InboundRouter.

    Routes by `job_id` through the vacancy registry. A response to a posting the
    registry does not know is skipped — but recorded, and replayed automatically
    once someone maps that id (see `catch_up_skipped`).

    Idempotent: re-running with the same last_id is safe; router dedupes by phone.
    """
    client = client or WorkUaClient()
    router = router or InboundRouter()
    stats = PollStats()
    cursor = _load_cursor()
    last_id: int | None = cursor.get("responses_last_id")
    allowed_vacancies = _allowed_vacancy_ids()

    # Before anything new: give back whatever a newly-mapped vacancy lost.
    try:
        if await catch_up_skipped(client=client, router=router, cursor=cursor):
            _save_cursor(cursor)
    except Exception as e:
        # A failed replay must never cost us the live poll — the ledger entry
        # survives, so the next tick tries again.
        log.warning("workua.catch_up.failed", error=str(e))

    try:
        # Always the account-wide feed, then route by `job_id` below.
        #
        # This used to call the per-vacancy endpoint once per allowed id. That
        # endpoint answers 404 for our postings, and the client turns 404 into an
        # empty page — so the poller reported new=0 with errors=0 and nobody
        # noticed for weeks while the other integration pulled from the same
        # account. Worse, work.ua issues a NEW job id when a posting is
        # republished, so a hardcoded id list goes stale silently by design.
        types = ["send", "phonecall"] if include_phonecalls else ["send"]
        page = await client.list_responses(
            limit=page_size, last_id=last_id, sort=1, from_types=types
        )
    except WorkUaAuthError as e:
        log.error("workua.auth_error", error=str(e))
        stats.errors += 1
        return stats
    except WorkUaRateLimitError as e:
        log.warning("workua.rate_limit", error=str(e))
        stats.errors += 1
        return stats
    except WorkUaApiError as e:
        log.error("workua.api_error", error=str(e))
        stats.errors += 1
        return stats

    items = page.get("items") or []
    stats.new_responses = len(items)
    if not items:
        log.info("workua.poll.no_new")
        _save_cursor(cursor)
        return stats

    # Responses to postings we do not know about. Counted and reported once per
    # poll instead of one info line each: a republished vacancy shows up here
    # as a live id nobody has mapped yet, which is exactly how 74% of the
    # work.ua flow went unnoticed.
    unknown_jobs: Counter = Counter()
    max_seen_id = last_id or 0
    for raw in items:
        try:
            resp = parse_response(raw)
        except Exception as e:
            log.warning("workua.parse_failed", error=str(e), raw_id=raw.get("id"))
            stats.errors += 1
            continue

        max_seen_id = max(max_seen_id, resp.id)

        if allowed_vacancies and resp.job_id not in allowed_vacancies:
            stats.rejected += 1
            unknown_jobs[resp.job_id] += 1
            # Remembered, not just counted: mapping the id later is enough to
            # get these people back.
            _record_skipped(cursor, job_id=resp.job_id, response_id=resp.id)
            continue

        await _ingest_response(resp, router=router, stats=stats)

    stats.last_id = max_seen_id
    cursor["responses_last_id"] = max_seen_id
    if unknown_jobs:
        # WARNING, not info: this is the signal that a posting was republished
        # under a new id and its applicants are going nowhere. Map it in the
        # vacancy registry (панель → Параметри вакансії → Збір і обдзвін) and
        # the next poll replays them on its own.
        log.warning(
            "workua.unknown_vacancies",
            jobs=dict(unknown_jobs.most_common()),
            skipped=sum(unknown_jobs.values()),
        )
    _save_cursor(cursor)
    log.info("workua.poll.done", **{k: getattr(stats, k) for k in stats.__dataclass_fields__})
    return stats


async def search_and_qualify(
    *,
    query: str,
    vacancy_id: int,
    vacancy_text: str,
    region_id: int | None = None,
    age_from: int = 22,
    age_to: int = 42,
    period: int = 3,
    limit: int = 20,
    scorer: MatchScorer | None = None,
    client: WorkUaClient | None = None,
    router: InboundRouter | None = None,
    vacancy_key: str | None = None,
    min_score: float = 0.55,
) -> dict[str, int]:
    """⚠️ PAID — every match opens a contact.

    Search for resumes matching `query`, then for each candidate run profile filter
    and embedding match before pushing to KeyCRM.

    `vacancy_key` decides which vacancy these people belong to: their funnel, the
    screening rules and the script Єва reads them. Without it a searched candidate
    lands on the default vacancy no matter what was searched for — the same gap
    the board pullers had until `candidates.vacancy_key` existed.
    """
    client = client or WorkUaClient()
    router = router or InboundRouter()
    scorer = scorer or MatchScorer()

    stats = {
        "found": 0,
        "profile_rejected": 0,
        "match_rejected": 0,
        "accepted": 0,
        "duplicates": 0,
        "errors": 0,
    }
    try:
        result = await client.search_resumes(
            search=query,
            region_id=region_id,
            age_from=age_from,
            age_to=age_to,
            with_phone=True,
            period=period,
            limit=limit,
        )
    except (WorkUaAuthError, WorkUaRateLimitError, WorkUaApiError) as e:
        log.error("workua.search_failed", error=str(e))
        stats["errors"] = 1
        return stats

    items = result.get("result") or []
    stats["found"] = len(items)
    for raw in items:
        try:
            resume = parse_resume({"result": raw})
        except Exception:
            stats["errors"] += 1
            continue
        if not resume.phone:
            stats["match_rejected"] += 1
            continue

        full_name = f"{resume.first_name or ''} {resume.last_name or ''}".strip()
        resume_text = json.dumps(resume.raw, ensure_ascii=False)
        birth_year = None
        if resume.birth_date and len(resume.birth_date) >= 4:
            try:
                birth_year = int(resume.birth_date[:4])
            except ValueError:
                birth_year = None

        profile = profile_evaluate(
            full_name=full_name,
            region=resume.region,
            desired_position=resume.name,
            last_position=(resume.experiences[0]["position"] if resume.experiences else None),
            resume_text=resume_text,
            experience_text=resume_text,
            birth_year=birth_year,
        )
        if not profile.accepted:
            stats["profile_rejected"] += 1
            continue

        try:
            score = await scorer.score(vacancy_text, resume_text[:4000])
        except Exception as e:
            log.warning("workua.match_failed", error=str(e))
            stats["match_rejected"] += 1
            continue

        if score.score < min_score:
            stats["match_rejected"] += 1
            continue

        route = vacancies.get(vacancy_key)
        ingest = await router.ingest(
            IngestPayload(
                full_name=full_name or "Кандидат work.ua",
                phone_raw=resume.phone,
                email=resume.email,
                region_raw=resume.region,
                desired_position=resume.name,
                source="workua_search",
                match_score=score.score,
                vacancy_id=vacancy_id,
                # Route the find like any other intake, or a searched candidate
                # ends up in the default funnel with the default script.
                vacancy_key=route.key,
                vacancy_name=route.label,
            )
        )
        if ingest.duplicate:
            stats["duplicates"] += 1
        elif ingest.accepted:
            stats["accepted"] += 1
        else:
            stats["match_rejected"] += 1

    log.info("workua.search.done", **stats)
    return stats
