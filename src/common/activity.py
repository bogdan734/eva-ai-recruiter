"""One line on the lead card: how many times Eva called and what she did in Telegram.

It opens the «AI Summary» field. The card's note would be the natural place, but
KeyCRM keeps a note as it was when the card was created and drops later edits,
so recruiters only ever saw this on a saved buyer's card (29.09).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from src.common.db import session_scope
from src.common.models import Call, Candidate

MARK = "🤖 Єва:"
TG_CHATTED = "листувалась"

# What a failed Telegram attempt says about the person. Anything else (daily
# limit, flood wait, "already wrote") says nothing about them and is not shown.
_TG_FAILURES = (
    (("не в Telegram",), "немає в Telegram"),
    (("приватність", "privacy", "PREMIUM"), "закрив повідомлення від незнайомих"),
)


def tg_sent_note(when: datetime) -> str:
    return f"написала {when:%d.%m}"


def tg_failed_note(error: str | None) -> str | None:
    text = error or ""
    for tokens, words in _TG_FAILURES:
        if any(t in text for t in tokens):
            return f"не вдалося написати — {words}"
    return None


def activity_line(*, calls: int, talked_sec: int, tg_note: str | None) -> str:
    parts = [f"дзвінків {calls}" if calls else "не телефонувала"]
    if talked_sec:
        parts.append(f"найдовша розмова {talked_sec // 60}:{talked_sec % 60:02d}")
    parts.append(f"Telegram: {tg_note or 'не писала'}")
    return f"{MARK} " + " · ".join(parts)


def with_activity(text: str | None, line: str) -> str:
    """The summary with `line` as its first line, instead of any older one."""
    rest = "\n".join(
        row for row in (text or "").splitlines() if not row.startswith(MARK)
    ).strip()
    return line + ("\n\n" + rest if rest else "")


async def line_for(candidate_id: int) -> str:
    """The line for one candidate, from the calls table and their Telegram note."""
    async with session_scope() as session:
        calls, talked = (await session.execute(
            select(func.count(Call.id), func.max(Call.duration_sec))
            .where(Call.candidate_id == candidate_id)
        )).one()
        cand = await session.get(Candidate, candidate_id)
        note = cand.tg_note if cand else None
    return activity_line(calls=int(calls or 0), talked_sec=int(talked or 0), tg_note=note)
