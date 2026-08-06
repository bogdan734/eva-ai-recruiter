"""Fill «Опис вакансії» and «Посилання на вакансію» on cards we created earlier.

Why this exists: cards made before the inbound path started sending resume text
and the applicant link are sitting empty in KeyCRM, while the data itself is in
our own `candidates` table all along. This copies it across.

What it will NOT do:
  * never overwrites a field that already has anything in it — a recruiter's own
    text always wins;
  * never touches title, stage, pipeline, contact or manager;
  * never creates a card.
  * «Номер вакансії» (LD_1002) is out of scope: it holds the work.ua response id,
    which we pass at creation time but never store, so there is nothing to copy.

Each card is re-read immediately before it is written, so the window in which a
recruiter could type into a field between our read and our write is a fraction of
a second rather than the length of the whole run. They are working this funnel
live; that matters.

Dry by default. Nothing is written without --apply.

    docker run --rm -v /opt/ai-recruiter:/app -w /app -e PYTHONPATH=/app \
      <image> python scripts/backfill_card_fields.py
    ... same, plus --apply, to actually write
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import httpx
from sqlalchemy import select

from src.common.db import session_scope
from src.common.keycrm import FIELD_RESUME_TEXT, FIELD_RESUME_URL
from src.common.models import Candidate
from src.common.settings import get_settings

# KeyCRM rate-limits around 60 requests/minute and answers 429 past that. The
# first run of this script paced only its writes and fired reads flat out — ~18
# req/s — and 55 of 78 cards came back 429. Every request now goes through
# _request(), which paces the whole run and honours Retry-After.
MIN_INTERVAL_SEC = 1.1          # ~55 req/min, just under the limit
RETRY_AFTER_FALLBACK_SEC = 20
MAX_RETRIES = 4
RESUME_TEXT_LIMIT = 8000

_last_call = 0.0


async def _request(client: httpx.AsyncClient, method: str, url: str, **kw):
    """One paced, 429-aware call. Returns the response, or None if it kept failing."""
    global _last_call
    for attempt in range(MAX_RETRIES):
        wait = MIN_INTERVAL_SEC - (asyncio.get_event_loop().time() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = asyncio.get_event_loop().time()

        r = await client.request(method, url, **kw)
        if r.status_code != 429:
            return r

        try:
            pause = float(r.headers.get("Retry-After") or RETRY_AFTER_FALLBACK_SEC)
        except ValueError:
            pause = RETRY_AFTER_FALLBACK_SEC
        pause = min(pause, 60) * (attempt + 1)
        print(f"  … rate limited, waiting {pause:.0f}s")
        await asyncio.sleep(pause)
    return None


async def _load_candidates() -> list[dict]:
    """Everyone who has a card and something worth copying onto it."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    Candidate.id,
                    Candidate.keycrm_lead_id,
                    Candidate.full_name,
                    Candidate.resume_text,
                    Candidate.work_ua_url,
                ).where(Candidate.keycrm_lead_id.is_not(None))
            )
        ).all()
    out = []
    for cid, lead_id, name, resume, url in rows:
        if not (resume or "").strip() and not (url or "").strip():
            continue
        out.append(
            {
                "candidate_id": cid,
                "lead_id": int(lead_id),
                "name": name or "",
                "resume_text": (resume or "").strip(),
                "work_ua_url": (url or "").strip(),
            }
        )
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N cards")
    args = ap.parse_args()

    s = get_settings()
    headers = {
        "Authorization": f"Bearer {s.keycrm_api_token}",
        "Accept": "application/json",
        "content-type": "application/json",
    }

    people = await _load_candidates()
    if args.limit:
        people = people[: args.limit]
    print(f"candidates with a card and data to copy: {len(people)}")
    print("MODE:", "APPLY — writing to KeyCRM" if args.apply else "DRY RUN — no writes")
    print()

    filled_resume = filled_url = 0
    already_ok = gone = failed = skipped_empty = 0

    async with httpx.AsyncClient(timeout=30) as client:
        for person in people:
            lead_id = person["lead_id"]

            # Re-read RIGHT BEFORE writing: recruiters are editing these cards
            # by hand while this runs.
            try:
                r = await _request(
                    client,
                    "GET",
                    f"{s.keycrm_base_url}/pipelines/cards/{lead_id}",
                    headers=headers,
                    params={"include": "custom_fields"},
                )
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  ! {lead_id} read failed: {e}")
                continue

            if r is None:
                failed += 1
                print(f"  ! {lead_id} read gave up after retries")
                continue
            if r.status_code == 404:
                gone += 1  # recruiter deleted it — nothing to do
                continue
            if r.status_code != 200:
                failed += 1
                print(f"  ! {lead_id} HTTP {r.status_code}")
                continue

            card = r.json()
            current = {
                f.get("uuid"): f.get("value") for f in (card.get("custom_fields") or [])
            }

            payload: list[dict] = []
            if person["resume_text"] and not current.get(FIELD_RESUME_TEXT):
                payload.append(
                    {
                        "uuid": FIELD_RESUME_TEXT,
                        "value": person["resume_text"][:RESUME_TEXT_LIMIT],
                    }
                )
            if person["work_ua_url"] and not current.get(FIELD_RESUME_URL):
                payload.append({"uuid": FIELD_RESUME_URL, "value": person["work_ua_url"]})

            if not payload:
                already_ok += 1
                continue

            what = ", ".join(
                "резюме" if p["uuid"] == FIELD_RESUME_TEXT else "посилання" for p in payload
            )
            title = (card.get("title") or person["name"])[:32]
            print(f"  {lead_id} | {title:<32} | {what}")

            for p in payload:
                if p["uuid"] == FIELD_RESUME_TEXT:
                    filled_resume += 1
                else:
                    filled_url += 1

            if not args.apply:
                continue

            try:
                w = await _request(
                    client,
                    "PUT",
                    f"{s.keycrm_base_url}/pipelines/cards/{lead_id}",
                    headers=headers,
                    json={"custom_fields": payload},
                )
                if w is None:
                    failed += 1
                    print(f"  ! {lead_id} write gave up after retries")
                elif w.status_code >= 300:
                    failed += 1
                    print(f"  ! {lead_id} write HTTP {w.status_code}: {w.text[:120]}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  ! {lead_id} write failed: {e}")

    print()
    print(f"{'written' if args.apply else 'would write'}:")
    print(f"  резюме    : {filled_resume}")
    print(f"  посилання : {filled_url}")
    print(f"already complete : {already_ok}")
    print(f"card deleted     : {gone}")
    print(f"errors           : {failed}")
    if skipped_empty:
        print(f"nothing to copy  : {skipped_empty}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
