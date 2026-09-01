"""Put the right «Джерело» on cards that were filed under work.ua by default.

Until 2026-09-01 nothing passed `source_id` when creating a card, so every one
of them carried the create_lead default — work.ua — no matter which board the
person came from. A recruiter filtering the funnel by robota.ua saw an empty
list and reported that robota.ua leads never reach the CRM. They reached it
wearing the wrong label.

New cards are labelled correctly now. This walks the ones already there.

    python scripts/relabel_sources.py                # dry run
    python scripts/relabel_sources.py --limit 2 --apply
    python scripts/relabel_sources.py --apply
    python scripts/relabel_sources.py --rollback state/relabelled_sources.json

Only cards whose current source is still the default get touched: a source a
human has since corrected by hand is a decision, not a mistake to overwrite.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from src.common.db import session_scope
from src.common.keycrm import DEFAULT_SOURCE_ID, KeyCRMClient, crm_source_id
from src.common.models import Candidate
from src.common.state import state_dir

LEDGER = "relabelled_sources.json"
# KeyCRM answers 202 immediately and applies the change seconds later; a single
# read reported a healthy write as a failure when the duplicates were parked.
SETTLE_CHECKS = (3, 4, 5, 8, 10)
SOURCE_NAMES = {1: "work.ua", 2: "rabota.ua", 3: "Анкети", 4: "Telegram"}


async def _wanted() -> list[tuple[int, str, int]]:
    """(card_id, name, intended_source_id) for cards whose label is wrong."""
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Candidate.keycrm_lead_id, Candidate.full_name, Candidate.source)
                .where(Candidate.keycrm_lead_id.isnot(None))
                .where(Candidate.source.isnot(None))
                .order_by(Candidate.id)
            )
        ).all()
    out = []
    for lead_id, name, source in rows:
        want = crm_source_id(source)
        if want != DEFAULT_SOURCE_ID:
            out.append((int(lead_id), name or "", want))
    return out


async def _set_source(client: KeyCRMClient, card_id: int, source_id: int) -> bool:
    await client.update_lead(card_id, {"source_id": source_id})
    for wait in SETTLE_CHECKS:
        await asyncio.sleep(wait)
        r = await client._get_rate_limited(f"/pipelines/cards/{card_id}", params={})
        if r.status_code == 200 and r.json().get("source_id") == source_id:
            return True
    return False


async def rollback(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    client = KeyCRMClient()
    ok = bad = 0
    try:
        for row in data.get("cards") or []:
            if await _set_source(client, int(row["id"]), int(row["from_source"])):
                ok += 1
            else:
                bad += 1
                print(f"  ⚠️ картка {row['id']} не повернулась")
    finally:
        await client.aclose()
    print(f"повернуто {ok}, не вдалось {bad}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rollback", type=str, default="")
    args = ap.parse_args()

    if args.rollback:
        await rollback(Path(args.rollback))
        return

    wanted = await _wanted()
    print(f"кандидатів з карткою і не-work.ua каналом: {len(wanted)}")

    client = KeyCRMClient()
    try:
        todo = []
        for card_id, name, want in wanted:
            r = await client._get_rate_limited(f"/pipelines/cards/{card_id}", params={})
            if r.status_code != 200:
                print(f"  ⚠️ картка {card_id} недоступна ({r.status_code})")
                continue
            now = r.json().get("source_id")
            if now == want:
                continue
            if now != DEFAULT_SOURCE_ID:
                # Someone set this by hand — leave their decision alone.
                print(f"  ⏭️  {card_id} {name}: джерело {now}, не типове — не чіпаю")
                continue
            todo.append({"id": card_id, "name": name, "to": want, "from_source": now})

        print(f"треба перепідписати: {len(todo)}")
        for row in todo[:5]:
            print(f"   {row['id']} {row['name']} → {SOURCE_NAMES.get(row['to'])}")
        if len(todo) > 5:
            print(f"   ... і ще {len(todo) - 5}")

        if not todo:
            return
        targets = todo[: args.limit] if args.limit else todo
        if not args.apply:
            print()
            print(f"DRY RUN. Перепідписало б {len(targets)}. Далі: --apply")
            return

        path = state_dir() / LEDGER
        done: list[dict] = []
        failed = 0
        for row in targets:
            if await _set_source(client, row["id"], row["to"]):
                done.append(row)
            else:
                failed += 1
                print(f"  ⚠️ картка {row['id']} не змінилась")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "cards": done,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        print()
        print(f"перепідписано {len(done)}, не вдалось {failed}")
        print(f"журнал відкату: {path}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
