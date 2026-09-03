"""Open the parked contacts of one vacancy, instead of waiting for the walker.

The backlog walker probes two entries per poll and opens at most a handful, and
it deliberately rotates so no one starves. With 181 parked applies that is the
right behaviour in general and useless when a recruiter is looking at twelve
specific unviewed responses on one posting today.

This takes a vacancy id, spends an opening on each of its parked applies, and
feeds whatever the number reveals through the normal intake — same routing, same
filters, same card.

    python scripts/open_vacancy_backlog.py 11249166
    python scripts/open_vacancy_backlog.py 11249166 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json

from src.api.inbound_router import InboundRouter
from src.common import vacancies
from src.common.state import state_dir
from src.integrations.robotaua_api import RobotaUaClient
from src.integrations.robotaua_sync import (
    RobotaUaPollStats,
    _pending_as_apply,
    _try_auto_open,
)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("vacancy_id", type=int)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    path = state_dir() / "robotaua_cursor.json"
    cursor = json.loads(path.read_text(encoding="utf-8"))
    pending = cursor.get("pending") or {}

    mine = {
        k: v for k, v in pending.items()
        if int(v.get("vacancy_id") or 0) == args.vacancy_id
    }
    route = vacancies.for_robotaua(args.vacancy_id)
    print(f"вакансія {args.vacancy_id}: {route.key if route else '❌ не в реєстрі'}")
    print(f"у черзі на відкриття: {len(mine)}")
    for k, v in list(mine.items())[:6]:
        print(f"   {v.get('name')} — {v.get('resume_type')}, з {str(v.get('first_seen'))[:10]}")
    if len(mine) > 6:
        print(f"   ... і ще {len(mine) - 6}")
    if not mine or route is None:
        return
    if not args.apply:
        print(f"\nDRY RUN. Відкрило б до {min(len(mine), args.limit)} контактів. Далі: --apply")
        return

    client = RobotaUaClient()
    router = InboundRouter()
    stats = RobotaUaPollStats()
    cities = await client.city_map()
    opened = skipped = 0
    try:
        for apply_id, entry in list(mine.items())[: args.limit]:
            apply = _pending_as_apply(apply_id, entry)
            ok = await _try_auto_open(
                client, router, apply, cities, cursor, stats, dry=False
            )
            if ok:
                pending.pop(apply_id, None)
                opened += 1
                print(f"   ✅ {entry.get('name')}")
            else:
                skipped += 1
                print(f"   ⏭️  {entry.get('name')} — не відкрито")
            await asyncio.sleep(3)
    finally:
        close = getattr(client, "aclose", None)
        if close:
            await close()

    cursor["pending"] = pending
    path.write_text(json.dumps(cursor, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nвідкрито {opened}, пропущено {skipped}")
    print(f"прийнято в інтейк: {stats.accepted}, дублів {stats.duplicates}")


if __name__ == "__main__":
    asyncio.run(main())
