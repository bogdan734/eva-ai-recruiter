"""Create the CRM cards that deferred mode skipped.

`DEFER_KEYCRM_UNTIL_QUALIFIED` withholds the card until Eva has qualified the
person on a call. Switching a vacancy to calls_enabled therefore stops its cards
appearing at intake — which is what happened to sales on 02.09, and why the
recruiter found work.ua applicants missing from the funnel while accountants,
whose vacancy is not called, kept arriving normally.

The setting is off now. These are the people who fell in the gap.

    python scripts/backfill_cards.py                # dry run
    python scripts/backfill_cards.py --limit 2 --apply
    python scripts/backfill_cards.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.common import vacancies
from src.common.db import session_scope
from src.common.keycrm import (
    DEFAULT_MANAGER_ID,
    FUNNEL_ID,
    STATUS_NEW,
    KeyCRMClient,
    crm_source_id,
)
from src.common.models import Candidate
from src.common.vacancy_link import vacancy_number_and_url

WINDOW_DAYS = 14


async def _cardless(limit: int) -> list[Candidate]:
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Candidate)
                .where(Candidate.keycrm_lead_id.is_(None))
                .where(Candidate.created_at >= since)
                .where(Candidate.phone_e164.isnot(None))
                .order_by(Candidate.id)
            )
        ).scalars().all()
        out = []
        for c in rows[: limit or None]:
            out.append(
                Candidate(
                    id=c.id, full_name=c.full_name, phone_e164=c.phone_e164,
                    email=c.email, source=c.source, vacancy_key=c.vacancy_key,
                    resume_text=c.resume_text, region=c.region,
                )
            )
        return out


async def _attach(candidate_id: int, lead_id: int) -> None:
    async with session_scope() as s:
        row = (
            await s.execute(select(Candidate).where(Candidate.id == candidate_id))
        ).scalar_one_or_none()
        if row:
            row.keycrm_lead_id = lead_id


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    people = await _cardless(args.limit)
    print(f"кандидатів без картки за {WINDOW_DAYS} днів: {len(people)}")
    for c in people[:6]:
        print(f"   {c.id} {c.full_name} ({c.vacancy_key or '—'})")
    if len(people) > 6:
        print(f"   ... і ще {len(people) - 6}")
    if not people:
        return
    if not args.apply:
        print(f"\nDRY RUN. Створило б {len(people)} карток. Далі: --apply")
        return

    client = KeyCRMClient()
    made = failed = 0
    try:
        for c in people:
            route = vacancies.get(c.vacancy_key)
            number, url = vacancy_number_and_url(c.source, None, route)
            try:
                created = await client.create_lead(
                    title=c.full_name,
                    full_name=c.full_name,
                    phone=c.phone_e164,
                    email=c.email,
                    vacancy_name=route.label,
                    vacancy_number=number,
                    vacancy_url=url,
                    resume_text=c.resume_text,
                    manager_comment=(
                        f"джерело: {c.source or '—'}"
                        + (f" | регіон: {c.region}" if c.region else "")
                        + " | картка створена догоном 03.09"
                    ),
                    pipeline_id=route.keycrm_pipeline_id or FUNNEL_ID,
                    status_id=route.keycrm_status_id or STATUS_NEW,
                    source_id=crm_source_id(c.source),
                    manager_id=DEFAULT_MANAGER_ID,
                    save_buyer=route.calls_enabled,
                )
                lead_id = int(created.get("id") or 0)
                if not lead_id:
                    failed += 1
                    print(f"   ⚠️ {c.full_name}: KeyCRM не повернув id")
                    continue
                await _attach(c.id, lead_id)
                made += 1
                print(f"   ✅ {c.full_name} → картка {lead_id}")
            except Exception as e:  # noqa: BLE001 — one bad row must not stop the run
                failed += 1
                print(f"   ⚠️ {c.full_name}: {str(e)[:110]}")
    finally:
        await client.aclose()

    print(f"\nстворено {made}, не вдалось {failed}")


if __name__ == "__main__":
    asyncio.run(main())
