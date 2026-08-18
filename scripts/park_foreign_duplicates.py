"""Park the other integration's duplicate cards out of the working column.

The foreign integration (IP 91.206.200.150) files every applicant it sees into
funnel 1 "Новий", where the recruiters work. Most of what it produces duplicates
what we already pull, so the column fills with people who already have a card.

Only two kinds of card are touched:
  * funnel 1, status "Новий" — nobody has worked it yet, so moving it destroys
    no human decision. A card a recruiter has already dragged anywhere else is
    left exactly where it is.
  * phone already present in our database — a confirmed duplicate.

That second rule is the important one. Of the 255 cards sitting in "Новий" on
2026-08-18, only 85 were duplicates: 149 were people ONLY the other integration
ever captured, because our own intake was dead for weeks. Parking those would
have hidden real candidates nobody else has. Doubt keeps the card visible.

    python scripts/park_foreign_duplicates.py                 # dry run
    python scripts/park_foreign_duplicates.py --limit 2 --apply
    python scripts/park_foreign_duplicates.py --apply
    python scripts/park_foreign_duplicates.py --rollback state/parked_duplicates.json

KeyCRM answers 202 {"status": true} to things it then ignores, and applies real
changes a few seconds late — so every move is re-read after a pause and counted
only if the card actually landed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from src.common.db import session_scope
from src.common.keycrm import KeyCRMClient
from src.common.models import Candidate
from src.common.phone import normalize_phone
from src.common.state import state_dir

WORKING_PIPELINE = 1
NEW_STATUS = 1        # "Новий" — the recruiters' inbox
PARK_STATUS = 32      # "Не актуально" — where 3485 of these already sit
LEDGER = "parked_duplicates.json"
# KeyCRM applies a status change a few seconds late and answers 202 immediately,
# so a single read after a fixed pause reports healthy moves as failures — it did
# exactly that for card 10475 on the first full run. Poll instead of guessing.
SETTLE_CHECKS = (3, 4, 5, 8, 10)


def _ledger_path() -> Path:
    return state_dir() / LEDGER


async def _our_phones() -> dict[str, tuple[str, str]]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Candidate.phone_e164, Candidate.status, Candidate.vacancy_key)
            )
        ).all()
    return {r[0]: (r[1] or "", r[2] or "") for r in rows}


def _card_phone(card: dict) -> str | None:
    contact = card.get("contact") or {}
    phones = contact.get("phone") or []
    if isinstance(phones, str):
        phones = [phones]
    for raw in phones:
        n = normalize_phone(raw) if raw else None
        if n:
            return n
    return None


async def _collect(client: KeyCRMClient, known: dict) -> tuple[list[dict], int, int]:
    """Cards in the inbox, split into duplicates / only-theirs / no-phone."""
    duplicates: list[dict] = []
    only_theirs = 0
    no_phone = 0
    page = 1
    while page <= 40:
        r = await client._get_rate_limited(
            "/pipelines/cards",
            params={
                "limit": 50,
                "page": page,
                "filter[pipeline_id]": WORKING_PIPELINE,
                "filter[status_id]": NEW_STATUS,
                "include": "contact",
            },
        )
        if r.status_code != 200:
            raise SystemExit(f"KeyCRM {r.status_code}: {r.text[:200]}")
        data = r.json().get("data") or []
        if not data:
            break
        for card in data:
            phone = _card_phone(card)
            if not phone:
                no_phone += 1
                continue
            if phone not in known:
                only_theirs += 1
                continue
            contact = card.get("contact") or {}
            duplicates.append(
                {
                    "id": card.get("id"),
                    "name": contact.get("full_name"),
                    "phone": phone,
                    "created_at": card.get("created_at"),
                    "from_status": card.get("status_id"),
                }
            )
        page += 1
    return duplicates, only_theirs, no_phone


async def _move(client: KeyCRMClient, card_id: int, status_id: int) -> bool:
    """Move one card and confirm it actually landed.

    A 202 from KeyCRM means "received", not "applied" — it answers the same way
    to requests it goes on to ignore. Only a re-read counts, and the re-read has
    to be patient: the change surfaces anywhere from two to fifteen seconds later.
    """
    await client.move_to_status(card_id, status_id)
    for wait in SETTLE_CHECKS:
        await asyncio.sleep(wait)
        if await client.get_card_status(card_id) == status_id:
            return True
    return False


async def rollback(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    client = KeyCRMClient()
    ok = failed = 0
    try:
        for row in data.get("cards") or []:
            if await _move(client, int(row["id"]), int(row["from_status"])):
                ok += 1
            else:
                failed += 1
                print(f"  ⚠️ картка {row['id']} не повернулась")
    finally:
        await client.aclose()
    print(f"повернуто {ok}, не вдалось {failed}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="без цього — лише показ")
    ap.add_argument("--limit", type=int, default=0, help="перенести не більше N (для проби)")
    ap.add_argument("--rollback", type=str, default="", help="файл журналу для відкату")
    args = ap.parse_args()

    if args.rollback:
        await rollback(Path(args.rollback))
        return

    known = await _our_phones()
    print(f"телефонів у нашій базі: {len(known)}")

    client = KeyCRMClient()
    try:
        duplicates, only_theirs, no_phone = await _collect(client, known)
        print(
            f"у «Новий» воронки {WORKING_PIPELINE}: "
            f"дублів {len(duplicates)}, тільки в них {only_theirs}, без телефону {no_phone}"
        )
        print("   тільки в них і без телефону НЕ чіпаємо — це або реальні кандидати,")
        print("   яких маємо лише через них, або картка, яку нема з чим звірити")

        if not duplicates:
            print("нічого переносити")
            return

        targets = duplicates[: args.limit] if args.limit else duplicates
        print()
        for row in targets[:5]:
            print(f"   {row['id']}  {row['name']}  {row['phone']}  {str(row['created_at'])[:10]}")
        if len(targets) > 5:
            print(f"   ... і ще {len(targets) - 5}")

        if not args.apply:
            print()
            print(f"DRY RUN. Перенесло б {len(targets)} карток у статус {PARK_STATUS}.")
            print("Щоб зробити: додати --apply (спершу варто --limit 2 --apply)")
            return

        path = _ledger_path()
        # Carry forward earlier runs: a --limit trial followed by the full run
        # must leave ONE ledger that can undo both, not just the last batch.
        previous: list[dict] = []
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8")).get("cards") or []
            except Exception:
                previous = []
        already = {int(r["id"]) for r in previous}
        moved: list[dict] = []
        failed: list[int] = []
        for row in targets:
            if await _move(client, int(row["id"]), PARK_STATUS):
                moved.append(row)
            else:
                failed.append(row["id"])
                print(f"  ⚠️ картка {row['id']} НЕ перенеслась — лишилась на місці")
            # Written after every card: an interrupted run must still be
            # reversible, and KeyCRM has no undo of its own.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "moved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "to_status": PARK_STATUS,
                        "cards": previous + [m for m in moved if int(m["id"]) not in already],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        print()
        print(f"перенесено {len(moved)}, не вдалось {len(failed)}")
        print(f"журнал для відкату: {path}")
        print(f"відкат: python scripts/park_foreign_duplicates.py --rollback {path}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
