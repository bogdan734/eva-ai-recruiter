"""Producer: feed scraped resumes through match-scoring + inbound router."""
from __future__ import annotations

import asyncio
from typing import Sequence

import structlog

from src.api.inbound_router import IngestPayload, InboundRouter
from src.match.profile_filter import evaluate as profile_evaluate
from src.match.scorer import MatchScorer
from src.scraper.workua import ResumeListing

log = structlog.get_logger()


def build_candidate_text(listing: ResumeListing) -> str:
    """How a candidate is described to the matcher.

    Shared with cold sourcing's pre-check so the two cannot disagree: the
    pre-check decides whether to spend a paid contact open, and this decides
    whether that was worth it. Different wording there meant paying for people
    this step then discarded (10.09.2026).
    """
    return (
        f"{listing.full_name or ''}\n"
        f"Бажана посада: {listing.desired_position or ''}\n"
        f"Регіон: {listing.region or ''}\n"
        f"Досвід: {listing.experience_years or 0} років\n"
        f"Мови: {', '.join(listing.languages)}\n"
    )


async def feed_resumes_to_pipeline(
    listings: Sequence[ResumeListing],
    *,
    vacancy_id: int,
    vacancy_text: str,
    vacancy_key: str | None = None,
    source: str = "workua_search",
    scorer: MatchScorer | None = None,
    router: InboundRouter | None = None,
    score_threshold: float = 0.65,
) -> dict[str, int]:
    """Score each listing and push qualified ones into the inbound router."""
    scorer = scorer or MatchScorer()
    router = router or InboundRouter()

    stats = {
        "received": 0,
        "profile_rejected": 0,
        "matched": 0,
        "ingested": 0,
        "duplicates": 0,
        "rejected": 0,
    }

    for listing in listings:
        stats["received"] += 1
        if not listing.phone_e164:
            stats["rejected"] += 1
            continue

        # Hard pre-filter: region/age/role auto-reject before expensive LLM call
        profile = profile_evaluate(
            full_name=listing.full_name,
            region=listing.region,
            desired_position=listing.desired_position,
            last_position=listing.desired_position,
            resume_text=" ".join(
                str(x) for x in [
                    listing.full_name,
                    listing.desired_position,
                    listing.region,
                ] if x
            ),
        )
        if not profile.accepted:
            log.info("profile.rejected", reason=profile.reason, url=listing.work_ua_url)
            stats["profile_rejected"] += 1
            continue

        candidate_text = build_candidate_text(listing)
        try:
            score = await scorer.score(vacancy_text, candidate_text)
        except Exception as e:
            log.warning("match.failed", error=str(e))
            stats["rejected"] += 1
            continue

        if score.score < score_threshold:
            # Logged with the number: a run where everyone scores 0.63 is a
            # threshold conversation, one where everyone scores 0.2 is not.
            log.info(
                "match.below_threshold",
                score=round(float(score.score), 3),
                threshold=score_threshold,
                position=listing.desired_position,
                url=listing.work_ua_url,
            )
            stats["rejected"] += 1
            continue
        stats["matched"] += 1

        payload_kwargs = dict(
            full_name=listing.full_name or "Без імені",
            phone_raw=listing.phone_e164,
            region_raw=listing.region,
            desired_position=listing.desired_position,
            experience_years=listing.experience_years,
            languages=listing.languages,
            work_ua_url=listing.work_ua_url,
            source=source,
            match_score=score.score,
            vacancy_id=vacancy_id,
            # Cold-sourced: nobody applied anywhere, this is Єва reaching out.
            # Never a "response" -- see IngestPayload.is_response's own docstring.
            is_response=False,
        )
        if vacancy_key:
            payload_kwargs["vacancy_key"] = vacancy_key
        result = await router.ingest(IngestPayload(**payload_kwargs))
        if not result.accepted:
            stats["rejected"] += 1
        elif result.duplicate:
            stats["duplicates"] += 1
        else:
            stats["ingested"] += 1
        await asyncio.sleep(0.1)

    log.info("queue_producer.done", **stats)
    return stats
