"""Regression tests for _finalize_call's "empty call after a real conversation"
guard (src/call/orchestrator.py).

Incident, 05.09.2026: a SIP-486 storm gave candidates 3099/3166/3181 an
empty-transcript retry right after an OLD call whose transcript was pure noise
("алло, добрий день" into a bad line, or a one-word non-answer -- never a real
screening). The guard used to treat ANY non-empty prior transcript as "already
had a real conversation, don't touch this candidate's disposition", so it
returned without ever moving `candidate.status` off CALLING -- all three sat
invisible in "calling" for hours. The fix requires spoke_with_candidate=True on
the prior call, the same signal the dispatcher's REAL_CONTACT_SEC guard uses.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.call.orchestrator import CallOrchestrator
from src.call.summarizer import CallSummary
from src.common.models import Base, Call, CallStatus, Candidate, CandidateStatus


class _FakeSummarizer:
    """Never called in the early-return path; used for the fall-through case."""

    async def summarize(self, **kwargs):
        return CallSummary(
            summary="Дзвінок не відбувся.",
            sentiment="neutral",
            objections=["none"],
            language="uk",
            qualified=False,
            spoke_with_candidate=False,
        )


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _make_candidate(maker, *, old_transcript: str, old_spoke: bool | None) -> int:
    async with maker() as session:
        cand = Candidate(
            full_name="Тест Тестовий", phone_e164="+380990000099",
            source="manual", status=CandidateStatus.CALLING, call_attempts=1,
        )
        session.add(cand)
        await session.flush()
        session.add(Call(
            candidate_id=cand.id, attempt_number=1, status=CallStatus.HANGUP,
            duration_sec=15, transcript=old_transcript,
            spoke_with_candidate=old_spoke,
        ))
        # The new, empty-transcript retry -- this is the row _finalize_call runs on.
        new_call = Call(
            candidate_id=cand.id, attempt_number=2, status=CallStatus.FAILED,
            vapi_call_id="test-call-2",
        )
        session.add(new_call)
        await session.commit()
        await session.refresh(new_call)
        return cand.id, new_call.id


@pytest.mark.asyncio
async def test_junk_old_transcript_does_not_block_disposition(db, monkeypatch):
    """Regression for 3099/3166/3181: an old call that's pure noise
    (spoke_with_candidate=False) must not be mistaken for a real conversation --
    today's dead attempt should still move the candidate off CALLING."""
    monkeypatch.setattr("src.call.orchestrator.session_scope", db, raising=False)
    # session_scope is used as `async with session_scope() as s`, so the fixture
    # itself (an async_sessionmaker) needs to be called with no args to produce
    # that context manager, same shape as the real one.
    cand_id, call_id = await _make_candidate(
        db, old_transcript="AI: … Алло! Добрий день!\nAI: Перепрошую, звʼязок тихий.",
        old_spoke=False,
    )

    orch = CallOrchestrator(summarizer=_FakeSummarizer())
    async with db() as session:
        db_call = await session.get(Call, call_id)
        candidate = await session.get(Candidate, cand_id)
        await orch._finalize_call(
            db_call=db_call, candidate=candidate, vacancy=None,
            transcript="", duration_sec=0, recording_url=None,
            ended_reason="customer-busy",
        )
        await session.commit()

    async with db() as session:
        candidate = await session.get(Candidate, cand_id)
        assert candidate.status != CandidateStatus.CALLING


@pytest.mark.asyncio
async def test_genuine_prior_conversation_still_protected(db, monkeypatch):
    """A real prior screening (spoke_with_candidate=True) must still stop a later
    dead attempt from being (mis)treated as this candidate's outcome -- the
    original bug this guard existed to fix must stay fixed."""
    monkeypatch.setattr("src.call.orchestrator.session_scope", db, raising=False)
    cand_id, call_id = await _make_candidate(
        db, old_transcript="AI: ... User: Так, працював менеджером з продажу два роки.",
        old_spoke=True,
    )

    orch = CallOrchestrator(summarizer=_FakeSummarizer())
    async with db() as session:
        db_call = await session.get(Call, call_id)
        candidate = await session.get(Candidate, cand_id)
        await orch._finalize_call(
            db_call=db_call, candidate=candidate, vacancy=None,
            transcript="", duration_sec=0, recording_url=None,
            ended_reason="customer-busy",
        )
        await session.commit()

    async with db() as session:
        call = await session.get(Call, call_id)
        assert call.status == CallStatus.FAILED
        assert call.ended_at is not None
        # The early-return path never runs the summarizer/disposition logic --
        # ai_summary stays unset, proving the dead attempt was ignored, not
        # treated as this candidate's real outcome.
        assert call.ai_summary is None
