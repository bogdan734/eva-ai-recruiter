"""Emergency: archive ALL leads on stage Новий (status_id=1) in KeyCRM funnel 1
to status_id=32 (Не актуально). Then wipe local DB candidates so poll can
re-fetch clean state.
"""
import asyncio
from src.common.keycrm import KeyCRMClient
from src.common.db import session_scope
from src.common.models import Candidate, Call
from sqlalchemy import delete


async def main():
    c = KeyCRMClient()
    # Fetch all leads on status_id=1
    novyj = []
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
        body = r.json()
        data = body.get("data", [])
        if not data:
            break
        novyj.extend(data)
        last_page = body.get("last_page") or 1
        if page >= last_page:
            break
        page += 1
    print(f"Found {len(novyj)} leads on Новий")

    archived = 0
    errors = 0
    for lead in novyj:
        lid = lead.get("id")
        try:
            await c.update_lead(lid, {"status_id": 32})
            archived += 1
        except Exception as e:
            errors += 1
            print(f"  fail {lid}: {e}")
    print(f"Archived: {archived}, errors: {errors}")

    # Local DB — wipe workua_response_* candidates so we can re-poll fresh
    async with session_scope() as s:
        # First delete calls for those candidates
        result = await s.execute(
            delete(Call).where(
                Call.candidate_id.in_(
                    __import__("sqlalchemy").select(Candidate.id).where(
                        Candidate.source.like("workua_%")
                    ).scalar_subquery()
                )
            )
        )
        print(f"deleted local calls: {result.rowcount}")
        result = await s.execute(
            delete(Candidate).where(Candidate.source.like("workua_%"))
        )
        print(f"deleted local workua candidates: {result.rowcount}")


asyncio.run(main())
