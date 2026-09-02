"""Pull call recordings off Vapi before its retention window closes.

The plan keeps fourteen days of call history. Everything older is gone from
their side — record and audio together — and the URL stored on our call row is a
short-lived signed link that stops working long before that.

That was found the expensive way: asked for the two best conversations of the
whole project, both from 20.07, and neither could be produced. The transcripts
survived because they live in our database; the audio did not, because it never
did.

So every finished call with a recording gets fetched to disk while it still
exists. A seven-minute mono wav is about 6 MB and the disk has tens of gigabytes
free, so there is no reason to be selective.

    python scripts/archive_recordings.py            # what would be fetched
    python scripts/archive_recordings.py --apply
"""
from __future__ import annotations

import argparse
import asyncio

import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from src.call.vapi_client import VapiClient
from src.common.db import session_scope
from src.common.models import Call

# Vapi keeps 14 days; fetch inside 12 to leave room for a job that misses a run.
WINDOW_DAYS = 12
# Relative to the repo root, never an absolute host path: inside the container
# the repo is mounted somewhere else entirely, so an absolute path writes into
# the container's own filesystem and disappears with it. The first run of this
# script reported "saved 1" and left nothing behind for exactly that reason.
DEST = Path(__file__).resolve().parents[1] / "recordings"


def _path_for(call_id: int, started: datetime | None) -> Path:
    day = (started or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return DEST / day / f"call-{call_id}.wav"


async def _pending() -> list[tuple[int, str, datetime]]:
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Call.id, Call.vapi_call_id, Call.started_at)
                .where(Call.vapi_call_id.isnot(None))
                .where(Call.started_at >= since)
                .where(Call.duration_sec > 0)
                .order_by(Call.started_at.desc())
            )
        ).all()
    return [(r[0], r[1], r[2]) for r in rows if not _path_for(r[0], r[2]).exists()]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    pending = await _pending()
    print(f"дзвінків без локального запису за {WINDOW_DAYS} днів: {len(pending)}")
    if not pending:
        return
    if not args.apply:
        for cid, _, started in pending[:8]:
            print(f"   call {cid} — {started:%Y-%m-%d %H:%M}")
        print(f"\nDRY RUN. Завантажило б {len(pending)}. Далі: --apply")
        return

    client = VapiClient()
    saved = gone = failed = 0
    try:
        for cid, vapi_id, started in pending:
            r = await client._client.get(f"/call/{vapi_id}")
            if r.status_code != 200:
                # Past the retention window, or never stored — not an error to
                # retry forever, just a call whose audio no longer exists.
                gone += 1
                continue
            art = (r.json().get("artifact") or {})
            # `recordingUrl` is the raw bucket path and answers
            # `InvalidArgument: Authorization` to anyone who fetches it. The
            # downloadable links are the presigned ones sitting beside it, and
            # they expire — which is the whole reason this job exists.
            url = art.get("presignedMonoUrl") or art.get("presignedStereoUrl")
            if not url:
                gone += 1
                continue
            dest = _path_for(cid, started)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                # A plain client: the Vapi auth header makes the bucket reject it.
                async with httpx.AsyncClient(timeout=180, follow_redirects=True) as plain:
                    audio = await plain.get(url)
                if audio.status_code != 200 or len(audio.content) < 5000:
                    failed += 1
                    continue
                dest.write_bytes(audio.content)
                saved += 1
            except Exception as e:  # noqa: BLE001 — one bad call must not stop the run
                failed += 1
                print(f"   ⚠️ call {cid}: {str(e)[:80]}")
    finally:
        close = getattr(client, "aclose", None)
        if close:
            await close()

    print(f"\nзбережено {saved}, недоступно у Vapi {gone}, помилок {failed}")
    print(f"каталог: {DEST}")


if __name__ == "__main__":
    asyncio.run(main())
