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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from src.api.inbound_router import IngestPayload, InboundRouter
from src.integrations.workua_api import (
    WorkUaApiError,
    WorkUaAuthError,
    WorkUaClient,
    WorkUaRateLimitError,
    parse_resume,
    parse_response,
)
from src.match.profile_filter import evaluate as profile_evaluate
from src.match.scorer import MatchScorer

log = structlog.get_logger()

CURSOR_PATH = Path(".cache/workua_cursor.json")


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
    if not CURSOR_PATH.exists():
        return {}
    try:
        return json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cursor(state: dict[str, Any]) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


async def poll_responses(
    *,
    client: WorkUaClient | None = None,
    router: InboundRouter | None = None,
    include_phonecalls: bool = True,
    page_size: int = 50,
) -> PollStats:
    """Pull new responses since last_id and feed them through InboundRouter.

    Idempotent: re-running with the same last_id is safe; router dedupes by phone.
    """
    client = client or WorkUaClient()
    router = router or InboundRouter()
    stats = PollStats()
    cursor = _load_cursor()
    last_id: int | None = cursor.get("responses_last_id")

    try:
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
        return stats

    max_seen_id = last_id or 0
    for raw in items:
        try:
            resp = parse_response(raw)
        except Exception as e:
            log.warning("workua.parse_failed", error=str(e), raw_id=raw.get("id"))
            stats.errors += 1
            continue

        if resp.id > max_seen_id:
            max_seen_id = resp.id

        if not resp.phone:
            stats.rejected += 1
            continue

        full_name = resp.fio or "Кандидат work.ua"
        # Quick profile filter — region/age via from_type isn't available here yet,
        # but birth_date is.
        birth_year = None
        if resp.birth_date and len(resp.birth_date) >= 4:
            try:
                birth_year = int(resp.birth_date[:4])
            except ValueError:
                birth_year = None

        profile = profile_evaluate(
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
            continue

        result = await router.ingest(
            IngestPayload(
                full_name=full_name,
                phone_raw=resp.phone,
                email=resp.email,
                region_raw=None,
                desired_position=None,
                source=f"workua_response_{resp.from_type}",
                vacancy_id=1,  # default local vacancy_id; work.ua id in raw payload
            )
        )
        if not result.accepted:
            stats.rejected += 1
        elif result.duplicate:
            stats.duplicates += 1
        else:
            stats.accepted += 1

    stats.last_id = max_seen_id
    cursor["responses_last_id"] = max_seen_id
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
) -> dict[str, int]:
    """⚠️ PAID — every match opens a contact.

    Search for resumes matching `query`, then for each candidate run profile filter
    and embedding match before pushing to KeyCRM.
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

        if score.score < 0.55:
            stats["match_rejected"] += 1
            continue

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
