"""The candidate row behind a call placed from the Telegram menu.

17.09.2026: the button dialled a real person through Vapi without creating any
local rows, so the end-of-call report had nothing to attach to
(`orchestrator.unknown_call`) and a 2m17s screening call that ended in "I will
pass you to the recruiter" produced no card. Whatever else changes, a call
placed at a named person must leave a row the pipeline can finish.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.bot.menu import _ensure_candidate_for_call
from src.common.models import Base, Candidate, CandidateStatus


def _autocommitting(maker):
    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session
            await session.commit()
    return scope


@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.common.db.session_scope", _autocommitting(maker))
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_named_call_creates_a_candidate_the_pipeline_can_finish(session_maker):
    cid = await _ensure_candidate_for_call(
        phone="+380674627262", name="Олена Тест", position="Менеджер з продажу", region="Дніпро"
    )
    async with session_maker() as s:
        cand = await s.get(Candidate, cid)
    assert cand is not None
    assert cand.phone_e164 == "+380674627262"
    assert cand.full_name == "Олена Тест"
    # IN_CALL_QUEUE is what the dispatcher and the end-of-call finalizer expect
    # to see; anything else and the call result has nowhere to land.
    assert cand.status == CandidateStatus.IN_CALL_QUEUE
    assert cand.source == "tg_manual_call"
    assert cand.vacancy_key == "sales"


@pytest.mark.asyncio
async def test_a_known_number_is_reused_never_duplicated(session_maker):
    async with session_maker() as s:
        s.add(Candidate(
            full_name="Вже Існує", phone_e164="+380501112233",
            source="workua_response_send", status=CandidateStatus.CLOSED,
        ))
        await s.commit()

    cid = await _ensure_candidate_for_call(
        phone="+380501112233", name="Інше Ім'я", position=None, region=None
    )

    async with session_maker() as s:
        rows = (await s.execute(select(Candidate).where(
            Candidate.phone_e164 == "+380501112233"))).scalars().all()
    assert len(rows) == 1, "a test dial must not duplicate a real candidate"
    assert rows[0].id == cid
    assert rows[0].full_name == "Вже Існує", "the existing record stays authoritative"
    # Re-queued so the call has somewhere to land, even though it was closed.
    assert rows[0].status == CandidateStatus.IN_CALL_QUEUE


@pytest.mark.asyncio
async def test_optional_fields_may_be_empty(session_maker):
    cid = await _ensure_candidate_for_call(
        phone="+380671234567", name="Без Деталей", position=None, region=None
    )
    async with session_maker() as s:
        cand = await s.get(Candidate, cid)
    assert cand.desired_position is None
    assert cand.region is None
    assert cand.status == CandidateStatus.IN_CALL_QUEUE
