"""Give back a call to the people whose phone never actually rang.

55 candidates have call rows that ALL ended in a telephony fault -- the 403
outage of 12.08-04.09 and the flaky route of 20-22.09. None of them ever heard
a ring, so none of them ever refused: the system gave up on their behalf.

But only some of them are Eva's to dial. Since 04.09.2026 a self-applied
candidate is never auto-called -- they came to us, so a human works the card.
Requeueing a work.ua responder would not be restoring a lost lead, it would be
breaking the rule the client set, and it would put Eva on the phone to people
the recruiter has already got in her own queue. So the SOURCE decides here, not
the status:

  * workua_search (cold sourcing)  -> Eva found them, Eva calls them -> requeue
  * *_response / *_chat            -> recruiter's, left alone, reported instead

call_attempts is reset to 0 rather than decremented: a fault the carrier caused
is not an attempt the candidate spent, and the same reasoning already lives in
orchestrator._finalize_call and in the dispatcher's connected-calls-only cap.
"""
import asyncio

from sqlalchemy import select, func, or_

from src.common.db import session_scope
from src.common.models import Candidate, Call, CandidateStatus

TECH = ("%error%", "%sip%forbidden%", "%failed-to-connect%")
SOURCED_MARKERS = ("workua_search", "robotaua_search")


async def main(commit: bool) -> None:
    async with session_scope() as session:
        tech_ids = select(Call.candidate_id).where(
            or_(*[Call.ended_reason.ilike(p) for p in TECH])
        ).distinct()

        spoke_ids = select(Call.candidate_id).where(
            or_(
                Call.spoke_with_candidate.is_(True),
                Call.duration_sec > 5,
                func.length(func.coalesce(Call.transcript, "")) > 40,
            )
        ).distinct()

        rows = (await session.execute(
            select(Candidate).where(
                Candidate.id.in_(tech_ids),
                Candidate.id.notin_(spoke_ids),
            ).order_by(Candidate.id)
        )).scalars().all()

        requeued, left = [], []
        for c in rows:
            src = (c.source or "").lower()
            if any(m in src for m in SOURCED_MARKERS):
                requeued.append(c)
            else:
                left.append(c)

        print(f"кандидатів, чиї дзвінки були ТІЛЬКИ технічними збоями: {len(rows)}")
        print(f"  повертаю в чергу (знайдені нами): {len(requeued)}")
        print(f"  лишаю рекрутеру (звернулись самі): {len(left)}")
        print()
        for c in requeued:
            print(f"  -> {c.id:>5} | {(c.full_name or '')[:28]:28} | {c.phone_e164} | "
                  f"{(c.region or '—')[:16]:16} | {c.status} -> in_call_queue | "
                  f"спроб {c.call_attempts} -> 0 | {c.source}")

        if not commit:
            print("\n[DRY RUN] нічого не змінено")
            return

        for c in requeued:
            c.status = CandidateStatus.IN_CALL_QUEUE
            c.call_attempts = 0
            c.callback_at = None
        print(f"\n[COMMIT] повернуто в чергу: {len(requeued)}")

        # The ones left alone still deserve to be countable -- a recruiter
        # asking "what happened to these people" should not have to take our
        # word for it.
        print("\nЗалишені рекрутеру, за статусом:")
        by_status: dict[str, int] = {}
        for c in left:
            k = str(getattr(c.status, "value", c.status))
            by_status[k] = by_status.get(k, 0) + 1
        for k, v in sorted(by_status.items(), key=lambda x: -x[1]):
            print(f"  {k:<16} {v}")


if __name__ == "__main__":
    import sys
    asyncio.run(main(commit="--commit" in sys.argv))
