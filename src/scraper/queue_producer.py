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


async def feed_resumes_to_pipeline(
    listings: Sequence[ResumeListing],
    *,
    vacancy_id: int,
    vacancy_text: str,
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

        candidate_text = (
            f"{listing.full_name or ''}\n"
            f"Бажана посада: {listing.desired_position or ''}\n"
            f"Регіон: {listing.region or ''}\n"
            f"Досвід: {listing.experience_years or 0} років\n"
            f"Мови: {', '.join(listing.languages)}\n"
        )
        try:
            score = await scorer.score(vacancy_text, candidate_text)
        except Exception as e:
            log.warning("match.failed", error=str(e))
            stats["rejected"] += 1
            continue

        if score.score < score_threshold:
            stats["rejected"] += 1
            continue
        stats["matched"] += 1

        result = await router.ingest(
            IngestPayload(
                full_name=listing.full_name or "Без імені",
                phone_raw=listing.phone_e164,
                region_raw=listing.region,
                desired_position=listing.desired_position,
                experience_years=listing.experience_years,
                languages=listing.languages,
                work_ua_url=listing.work_ua_url,
                source="workua_scraper",
                match_score=score.score,
                vacancy_id=vacancy_id,
            )
        )
        if not result.accepted:
            stats["rejected"] += 1
        elif result.duplicate:
            stats["duplicates"] += 1
        else:
            stats["ingested"] += 1
        await asyncio.sleep(0.1)

    log.info("queue_producer.done", **stats)
    return stats
