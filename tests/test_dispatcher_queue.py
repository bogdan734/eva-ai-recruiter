"""Regression tests for the dispatcher's dialable-candidate query.

Covers two incidents found on 05.09.2026:

1. HARD_CALL_CAP counted every Call row regardless of whether the carrier ever
   actually connected it, so a run of zero-duration failures (SIP-403 outage,
   or an older untagged batch) permanently stranded a candidate who had spent
   only one real attempt (candidates 3099/3166).
2. REAL_CONTACT_SEC treated any call >= 40s as "already had a real conversation,
   never redial" on duration alone -- but a 64s call that was entirely "алло, я
   вас не чую" with the screening never started is not a real conversation
   (candidate 3181, Цвігун). The fix requires the summarizer's own
   `spoke_with_candidate` judgment, not just duration.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.common.models import Base, Call, CallStatus, Candidate, CandidateStatus
from src.scheduler.dispatcher import HARD_CALL_CAP, dialable_candidates_query


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@dataclass
class _FakeSettings:
    """dialable_candidates_query() only ever reads these two fields -- a real
    get_settings() is a cached, environment-backed singleton and mutating it
    here would leak into other tests, so a tiny stand-in is safer."""
    call_max_attempts: int = 3
    call_max_concurrent: int = 10


def _settings():
    return _FakeSettings()


def _candidate(**kw) -> Candidate:
    defaults = dict(
        full_name="Тест Тестовий",
        phone_e164=f"+38000000{kw.get('id', 0):04d}",
        source="manual",
        status=CandidateStatus.IN_CALL_QUEUE,
        call_attempts=1,
    )
    defaults.update(kw)
    defaults.pop("id", None)
    return Candidate(**defaults)


async def _dialable_ids(session, s) -> set[int]:
    rows = (await session.execute(dialable_candidates_query(s))).scalars().all()
    return {c.id for c in rows}


@pytest.mark.asyncio
async def test_zero_duration_calls_never_exhaust_the_hard_cap(session):
    """Regression for candidates 3099/3166: N calls that never connected must
    not count against HARD_CALL_CAP, however many of them there are."""
    cand = _candidate(phone_e164="+380990000001", call_attempts=1)
    session.add(cand)
    await session.flush()
    for _ in range(HARD_CALL_CAP + 2):
        session.add(Call(
            candidate_id=cand.id, attempt_number=1, status=CallStatus.FAILED,
            duration_sec=0,
        ))
    await session.commit()

    assert cand.id in await _dialable_ids(session, _settings())


@pytest.mark.asyncio
async def test_connected_calls_do_exhaust_the_hard_cap(session):
    """The cap must still bite for someone who really was dialled repeatedly.

    Uses the real HARD_CALL_CAP constant rather than a guessed number, since
    it's configurable via the HARD_CALL_CAP env var and the deployed value
    (6, as of 05.09.2026) differs from the code's own documented default (4)."""
    cand = _candidate(phone_e164="+380990000002", call_attempts=1)
    session.add(cand)
    await session.flush()
    for _ in range(HARD_CALL_CAP):
        session.add(Call(
            candidate_id=cand.id, attempt_number=1, status=CallStatus.FAILED,
            duration_sec=5,
        ))
    await session.commit()

    assert cand.id not in await _dialable_ids(session, _settings())


@pytest.mark.asyncio
async def test_long_call_without_screening_does_not_block_a_retry(session):
    """Regression for candidate 3181 (Цвігун): a long call that never got past
    "алло, я вас не чую" (spoke_with_candidate=False) must not be treated as a
    finished conversation -- she still has an attempt to spend."""
    cand = _candidate(phone_e164="+380990000003", call_attempts=1)
    session.add(cand)
    await session.flush()
    session.add(Call(
        candidate_id=cand.id, attempt_number=1, status=CallStatus.HANGUP,
        duration_sec=64, ended_reason="customer-ended-call",
        spoke_with_candidate=False,
    ))
    await session.commit()

    assert cand.id in await _dialable_ids(session, _settings())


@pytest.mark.asyncio
async def test_long_call_with_real_screening_blocks_a_retry(session):
    """A genuinely screened, long call must still stop redialling -- the fix
    must not make the guard toothless."""
    cand = _candidate(phone_e164="+380990000004", call_attempts=1)
    session.add(cand)
    await session.flush()
    session.add(Call(
        candidate_id=cand.id, attempt_number=1, status=CallStatus.HANGUP,
        duration_sec=64, ended_reason="customer-ended-call",
        spoke_with_candidate=True,
    ))
    await session.commit()

    assert cand.id not in await _dialable_ids(session, _settings())


@pytest.mark.asyncio
async def test_legacy_call_missing_the_flag_does_not_block(session):
    """Calls finalized before this column existed have spoke_with_candidate=NULL.
    Unknown must not be held against the candidate, same as everywhere else this
    flag is read."""
    cand = _candidate(phone_e164="+380990000005", call_attempts=1)
    session.add(cand)
    await session.flush()
    session.add(Call(
        candidate_id=cand.id, attempt_number=1, status=CallStatus.HANGUP,
        duration_sec=64, ended_reason="customer-ended-call",
        spoke_with_candidate=None,
    ))
    await session.commit()

    assert cand.id in await _dialable_ids(session, _settings())


@pytest.mark.asyncio
async def test_short_call_never_blocks_regardless_of_screening_flag(session):
    """Below REAL_CONTACT_SEC the screening flag is irrelevant -- a short call
    was never going to be mistaken for a finished conversation."""
    cand = _candidate(phone_e164="+380990000006", call_attempts=1)
    session.add(cand)
    await session.flush()
    session.add(Call(
        candidate_id=cand.id, attempt_number=1, status=CallStatus.HANGUP,
        duration_sec=10, ended_reason="customer-ended-call",
        spoke_with_candidate=True,
    ))
    await session.commit()

    assert cand.id in await _dialable_ids(session, _settings())


@pytest.mark.asyncio
async def test_a_due_callback_is_dialled_even_after_a_real_conversation(session):
    """29.09: "bad line, call me in half an hour" after 87 seconds of real
    screening (Тимків), and a call cut mid-screening (Долгош). The real-contact
    guard kept both out of the queue for good, so the callback they asked for
    never came. A callback that is due is an explicit request -- it gets dialled."""
    from datetime import datetime, timedelta

    cand = _candidate(phone_e164="+380990000009", call_attempts=2,
                      callback_at=datetime.utcnow() - timedelta(minutes=5))
    session.add(cand)
    await session.flush()
    session.add(Call(
        candidate_id=cand.id, attempt_number=1, status=CallStatus.HANGUP,
        duration_sec=87, spoke_with_candidate=True,
    ))
    await session.flush()

    assert cand.id in await _dialable_ids(session, _FakeSettings(call_max_attempts=2))


@pytest.mark.asyncio
async def test_a_callback_not_yet_due_still_waits(session):
    from datetime import datetime, timedelta

    cand = _candidate(phone_e164="+380990000010", call_attempts=1,
                      callback_at=datetime.utcnow() + timedelta(hours=3))
    session.add(cand)
    await session.flush()
    session.add(Call(
        candidate_id=cand.id, attempt_number=1, status=CallStatus.HANGUP,
        duration_sec=87, spoke_with_candidate=True,
    ))
    await session.flush()

    assert cand.id not in await _dialable_ids(session, _settings())
