"""Cleanup Новий leads — direct httpx PUT with rate limiting + progress."""
import asyncio
import sys
from src.common.keycrm import KeyCRMClient
from src.common.db import session_scope
from src.common.models import Candidate, Call
from sqlalchemy import delete, select


async def fetch_all_novyj(c: KeyCRMClient) -> list[dict]:
    out = []
    page = 1
    while True:
        r = await c._client.get(
            "/pipelines/cards",
            params={
                "filter[pipeline_id]": 1,
                "filter[status_id]": 1,
                "limit": 50,
                "page": page,
            },
        )
        data = r.json().get("data", [])
        if not data:
            break
        out.extend(data)
        if len(data) < 50:
            break
        page += 1
        if page > 200:
            break
    return out


async def archive_batch(c: KeyCRMClient, leads: list[dict]) -> tuple[int, int]:
    archived = 0
    errors = 0
    for i, lead in enumerate(leads):
        lid = lead.get("id")
        try:
            r = await c._client.put(
                f"/pipelines/cards/{lid}",
                json={"status_id": 32},
                timeout=15.0,
            )
            if r.status_code in (200, 202):
                archived += 1
            else:
                errors += 1
                if errors <= 5:
                    print(f"  fail {lid}: HTTP {r.status_code}: {r.text[:150]}", flush=True)
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  fail {lid}: EXC {type(e).__name__}: {str(e)[:100]}", flush=True)
        if (i + 1) % 100 == 0:
            print(f"  progress: {i+1}/{len(leads)} — archived={archived} err={errors}", flush=True)
        # gentle throttle: 20 req/sec
        await asyncio.sleep(0.05)
    return archived, errors


async def main():
    c = KeyCRMClient()
    print("Fetching all Новий leads...", flush=True)
    novyj = await fetch_all_novyj(c)
    print(f"Found {len(novyj)} leads on Новий", flush=True)

    archived, errors = await archive_batch(c, novyj)
    print(f"\nDONE — archived: {archived}, errors: {errors}", flush=True)

    # Wipe local workua candidates so poll re-fetches
    async with session_scope() as s:
        cand_ids = (await s.execute(
            select(Candidate.id).where(Candidate.source.like("workua_%"))
        )).scalars().all()
        if cand_ids:
            await s.execute(delete(Call).where(Call.candidate_id.in_(cand_ids)))
            await s.execute(delete(Candidate).where(Candidate.id.in_(cand_ids)))
            print(f"wiped {len(cand_ids)} local workua_* candidates + their calls", flush=True)


asyncio.run(main())
