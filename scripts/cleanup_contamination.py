"""Remove leads ingested via account-wide poll (before per-vacancy fix landed).

Cutoff: 09:51:00 UTC today. Candidates created before that came from the reset-
cursor poll that used the general list_responses endpoint and pulled in
responses to deactivated 2018 vacancies (job_id 3321786 etc).
"""
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, delete
from src.common.db import session_scope
from src.common.keycrm import KeyCRMClient
from src.common.models import Candidate, Call


CUTOFF = datetime(2026, 7, 2, 9, 51, 0, tzinfo=timezone.utc)


async def main():
    c = KeyCRMClient()
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Candidate).where(
                    Candidate.source.like("workua_%"),
                    Candidate.created_at < CUTOFF,
                )
            )
        ).scalars().all()
    print(f"contamination candidates: {len(rows)}")

    archived = 0
    errors = 0
    lead_ids = [r.keycrm_lead_id for r in rows if r.keycrm_lead_id]
    print(f"lead_ids to archive: {len(lead_ids)}")

    for lid in lead_ids:
        for attempt in range(6):
            try:
                r = await c._client.put(
                    f"/pipelines/cards/{lid}",
                    json={"status_id": 32},
                    timeout=15.0,
                )
                if r.status_code in (200, 202):
                    archived += 1
                    break
                if r.status_code == 429:
                    await asyncio.sleep(1.5 * (2 ** attempt))
                    continue
                errors += 1
                break
            except Exception:
                await asyncio.sleep(0.5 + attempt)
        else:
            errors += 1
        await asyncio.sleep(0.5)
    print(f"archived: {archived}, errors: {errors}")

    # Wipe local
    ids = [r.id for r in rows]
    if ids:
        async with session_scope() as s:
            await s.execute(delete(Call).where(Call.candidate_id.in_(ids)))
            await s.execute(delete(Candidate).where(Candidate.id.in_(ids)))
        print(f"wiped local candidates: {len(ids)}")


asyncio.run(main())
