"""CLI over `src.integrations.tg_outreach` — see that module for the why.

    python scripts/outreach_backfill.py --created-after 2026-08-18T13:55
    python scripts/outreach_backfill.py --created-after 2026-08-18T13:55 --send --max 3
    python scripts/outreach_backfill.py --created-after 2026-08-18T13:55 --send

`--created-after` is required on purpose: without it the natural query is "every
sales candidate we never called", which is hundreds of people here, including
everyone Eva already spoke to months ago.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from src.integrations.tg_outreach import pending_candidates, run_once

_ICONS = {"sent": "✅", "skipped": "⏭️ ", "stopped": "⛔"}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vacancy", default="sales")
    ap.add_argument("--created-after", required=True, help="ISO, напр. 2026-08-18T13:55")
    ap.add_argument("--max", type=int, default=100, help="стеля на цей запуск")
    ap.add_argument("--send", action="store_true", help="без цього — лише показ")
    args = ap.parse_args()

    created_after = datetime.fromisoformat(args.created_after)
    if created_after.tzinfo is None:
        created_after = created_after.replace(tzinfo=UTC)

    people = await pending_candidates(args.vacancy, created_after, args.max)
    print(f"чекають на повідомлення (до {args.max}): {len(people)}")
    for _, name, phone in people[:5]:
        print(f"   {name}  {phone}")
    if len(people) > 5:
        print(f"   ... і ще {len(people) - 5}")
    if not people:
        return
    if not args.send:
        print()
        print("DRY RUN. Щоб надіслати: --send (спершу варто --max 3 --send)")
        return

    def report(kind: str, name: str, err: str) -> None:
        print(f"   {_ICONS.get(kind, '')} {name}{(' — ' + err) if err else ' — надіслано'}")

    stats = await run_once(
        vacancy=args.vacancy,
        created_after=created_after,
        limit=args.max,
        send=True,
        on_event=report,
    )
    print()
    print(f"надіслано {stats.sent}, пропущено назавжди {stats.skipped}")
    if stats.stopped_on:
        print(f"зупинився на: {stats.stopped_on} — решта піде наступного разу")


if __name__ == "__main__":
    asyncio.run(main())
