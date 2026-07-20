"""Manual candidate import — alternative to the work.ua scraper.

Accepts free-text lines or CSV rows and inserts callable candidates
(status IN_CALL_QUEUE) so the existing scheduler dispatches them.

Line/row format (comma or tab separated), phone first, rest optional:
    +380671234567, Олег, Дніпро, менеджер
    0671234567;Ірина;Львів
    380507654321
Header row (phone/name/region/position in any case) is auto-skipped.
"""
from __future__ import annotations

import re

from sqlalchemy import select

from src.common.db import session_scope
from src.common.models import Candidate, CandidateStatus


def normalize_phone(raw: str) -> str | None:
    """Return +380XXXXXXXXX or None if not a plausible UA mobile."""
    d = re.sub(r"\D", "", raw or "")
    if d.startswith("380") and len(d) == 12:
        return "+" + d
    if d.startswith("0") and len(d) == 10:
        return "+38" + d
    if len(d) == 9:  # bare 9 digits -> assume +380
        return "+380" + d
    return None


def _split(line: str) -> list[str]:
    for sep in ("\t", ";", ",", "|"):
        if sep in line:
            return [p.strip() for p in line.split(sep)]
    return [line.strip()]


async def import_from_lines(text: str) -> dict:
    """Parse text (one candidate per line) and insert. Returns a summary."""
    added, skipped_dup, skipped_bad = 0, 0, 0
    bad_samples: list[str] = []

    async with session_scope() as session:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = _split(line)
            phone = normalize_phone(parts[0])
            # skip header row
            if phone is None and parts and parts[0].lower() in ("phone", "телефон", "номер", "phone_e164"):
                continue
            if phone is None:
                skipped_bad += 1
                if len(bad_samples) < 3:
                    bad_samples.append(line[:40])
                continue
            name = parts[1] if len(parts) > 1 and parts[1] else "невідомий"
            region = parts[2] if len(parts) > 2 and parts[2] else None
            position = parts[3] if len(parts) > 3 and parts[3] else None

            exists = (
                await session.execute(select(Candidate).where(Candidate.phone_e164 == phone))
            ).scalar_one_or_none()
            if exists:
                skipped_dup += 1
                continue
            session.add(
                Candidate(
                    full_name=name,
                    phone_e164=phone,
                    region=region,
                    desired_position=position,
                    source="manual",
                    status=CandidateStatus.IN_CALL_QUEUE.value,
                )
            )
            added += 1

    return {
        "added": added,
        "skipped_dup": skipped_dup,
        "skipped_bad": skipped_bad,
        "bad_samples": bad_samples,
    }
