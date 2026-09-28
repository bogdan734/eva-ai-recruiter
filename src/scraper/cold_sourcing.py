"""Cold sourcing orchestration: search work.ua's resume database for each
calls_enabled vacancy and feed matches into Єва's call queue.

Distinct from the "response" pollers (workua_sync.py / robotaua_sync.py),
which only ever handle people who applied to a posting themselves and always
route to manager_review (see IngestPayload.is_response). This is the other
half — proactively finding people who never applied anywhere, for Єва to call
cold. IngestPayload.is_response's own docstring already anticipated a
"workua_search" source; this module is what was missing to make it real.

06.09.2026: wired up after candidates 3099/3166/3181 exhausted their attempts
and the call queue hit zero with no automated way to refill it — see the
09.05-09.06 thread on why the queue was empty and why the existing
`feed_resumes_to_pipeline()` had never actually run.
"""
from __future__ import annotations

import structlog

from src.common.settings import get_settings
from src.common.vacancies import LOCAL_FK, all_vacancies
from src.scraper.queue_producer import build_candidate_text, feed_resumes_to_pipeline
from src.integrations.workua_resume_search import search_resumes_via_api
from src.match.profile_filter import evaluate as profile_evaluate
from src.match.scorer import MatchScorer
from src.scraper.workua import search_resumes

log = structlog.get_logger()

_STATS_KEYS = (
    "received", "profile_rejected", "matched", "ingested", "duplicates", "rejected",
)


async def run_cold_sourcing_cycle() -> dict:
    """One cold-sourcing pass across every calls_enabled, intake_enabled vacancy.

    Total candidates actually fed into the queue are capped at
    `workua_cold_sourcing_max_per_run` regardless of how many vacancies exist —
    shared across vacancies, spent in registry order — so one run can never
    flood Єва's queue. Safe to call even with no session file on disk: each
    per-vacancy search just returns [] and this returns a "nothing to do"
    result instead of raising.
    """
    s = get_settings()
    if not s.workua_cold_sourcing_enabled:
        log.info("cold_sourcing.disabled")
        return {"skipped": "disabled"}
    from src.bot.admin import calls_paused
    if calls_paused():
        # /pause in the bot stops Eva reaching out — and every sourced person
        # costs a paid work.ua open to feed calls that would not happen.
        log.info("cold_sourcing.paused")
        return {"skipped": "paused"}

    remaining = s.workua_cold_sourcing_max_per_run
    # One allowance for the whole run, not one per vacancy: work.ua counts
    # contact opens per account per day, and so must we.
    opens_left = s.workua_max_contact_opens_per_run
    scorer = MatchScorer()
    totals = {k: 0 for k in _STATS_KEYS}
    # Search counters summed across vacancies, for the daily report.
    sourcing_tally: dict[str, int] = {}
    per_vacancy: dict[str, dict] = {}

    for key, vacancy in all_vacancies().items():
        if remaining <= 0:
            log.info("cold_sourcing.budget_exhausted", stopped_at=key)
            break
        if not vacancy.calls_enabled or not vacancy.intake_enabled:
            continue
        if not vacancy.role_markers:
            log.info("cold_sourcing.no_role_markers", vacancy=key)
            per_vacancy[key] = {"skipped": "no_role_markers"}
            continue

        # role_markers are free-text words already curated per vacancy for
        # exactly this kind of matching (src/common/vacancies.py) — reused as
        # search queries instead of maintaining a second list that could drift
        # out of sync with them.
        queries = list(vacancy.role_markers)
        want = min(remaining, s.workua_scrape_daily_limit)
        try:
            if s.workua_cold_sourcing_use_api:
                if opens_left <= 0:
                    log.info("cold_sourcing.opens_exhausted", stopped_at=key)
                    break

                vacancy_text = f"{vacancy.label}\n{vacancy.spoken_pitch}".strip()

                async def _prescreen(listing, _text=vacancy_text) -> bool:
                    """Same two gates queue_producer applies, run here where
                    rejecting somebody is still free."""
                    profile = profile_evaluate(
                        full_name=listing.full_name,
                        region=listing.region,
                        desired_position=listing.desired_position,
                        last_position=listing.desired_position,
                        resume_text=" ".join(
                            str(x) for x in (listing.full_name, listing.desired_position, listing.region) if x
                        ),
                    )
                    if not profile.accepted:
                        return False
                    try:
                        score = await scorer.score(_text, build_candidate_text(listing))
                    except Exception:
                        return False
                    ok = score.score >= s.cold_sourcing_match_threshold
                    log.info(
                        "cold_sourcing.prescreen",
                        score=round(float(score.score), 3),
                        keep=ok,
                        position=listing.desired_position,
                        region=listing.region,
                    )
                    return ok

                # Free listing -> both filters -> paid open for survivors only.
                # Cloudflare never enters into it.
                listings = await search_resumes_via_api(
                    queries=queries,
                    total_limit=min(want, opens_left),
                    open_budget=opens_left,
                    prescreen=_prescreen,
                    tally=sourcing_tally,
                )
                opens_left -= len(listings)
            else:
                listings = await search_resumes(
                    queries=queries,
                    total_limit=want,
                )
        except Exception as e:  # noqa: BLE001 — one vacancy's search failing must
            # not cost the others their turn in the same run.
            log.warning("cold_sourcing.search_failed", vacancy=key, error=str(e))
            per_vacancy[key] = {"error": str(e)}
            continue

        if not listings:
            per_vacancy[key] = {"found": 0}
            continue

        stats = await feed_resumes_to_pipeline(
            listings[:remaining],
            vacancy_id=LOCAL_FK,
            vacancy_text=f"{vacancy.label}\n{vacancy.spoken_pitch}".strip(),
            vacancy_key=key,
            # Same bar as the pre-check above, or we would pay to open a contact
            # and then throw the person away one step later.
            score_threshold=s.cold_sourcing_match_threshold,
        )
        per_vacancy[key] = stats
        for k in _STATS_KEYS:
            totals[k] += stats.get(k, 0)
        remaining -= stats.get("ingested", 0)

    # One breadcrumb for the whole cycle, after every vacancy has had its turn.
    if sourcing_tally:
        from src.integrations.workua_resume_search import record_cold_sourcing_counts

        record_cold_sourcing_counts(sourcing_tally)

    log.info("cold_sourcing.cycle_done", **totals, remaining_budget=remaining, opens_left=opens_left)
    return {"totals": totals, "by_vacancy": per_vacancy}
