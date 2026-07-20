"""Cleanup Новий leads — rate-limit aware with backoff on 429."""
import asyncio
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


async def archive_one(c: KeyCRMClient, lid: int) -> str:
    for attempt in range(6):
        try:
            r = await c._client.put(
                f"/pipelines/cards/{lid}",
                json={"status_id": 32},
                timeout=15.0,
            )
            if r.status_code in (200, 202):
                return "ok"
            if r.status_code == 429:
                # exponential backoff on rate limit
                await asyncio.sleep(1.5 * (2 ** attempt))
                continue
            return f"http_{r.status_code}"
        except Exception as e:
            await asyncio.sleep(0.5 + attempt)
    return "exhausted"


async def main():
    c = KeyCRMClient()
    print("Fetching all Новий leads...", flush=True)
    novyj = await fetch_all_novyj(c)
    print(f"Found {len(novyj)} leads on Новий", flush=True)

    archived = 0
    errors = 0
    for i, lead in enumerate(novyj):
        lid = lead.get("id")
        result = await archive_one(c, lid)
        if result == "ok":
            archived += 1
        else:
            errors += 1
            if errors <= 10:
                print(f"  fail {lid}: {result}", flush=True)
        if (i + 1) % 50 == 0:
            print(
                f"  progress: {i+1}/{len(novyj)} — archived={archived} err={errors}",
                flush=True,
            )
        # 500 ms between calls = 2/s, safe under KeyCRM limit
        await asyncio.sleep(0.5)

    print(f"\nDONE — archived: {archived}, errors: {errors}", flush=True)

    async with session_scope() as s:
        cand_ids = (
            await s.execute(
                select(Candidate.id).where(Candidate.source.like("workua_%"))
            )
        ).scalars().all()
        if cand_ids:
            await s.execute(delete(Call).where(Call.candidate_id.in_(cand_ids)))
            await s.execute(delete(Candidate).where(Candidate.id.in_(cand_ids)))
            print(f"wiped {len(cand_ids)} local workua_* candidates + calls", flush=True)


asyncio.run(main())
