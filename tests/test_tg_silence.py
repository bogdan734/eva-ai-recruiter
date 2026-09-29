"""A Telegram chat that went quiet does not sit in «В роботі» forever.

29.09 the client asked what Eva does with «В роботі» cards whose candidate stopped
answering in Telegram: nothing, ever. The owner's rule since: after two silent
days Eva sends one reminder, after three more the card goes to «Не актуально»,
and someone who writes back after that is picked up again.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api import tg_gate, tg_silence
from src.api.tg_silence import handle_tg_silence
from src.common.models import Base, Candidate, CandidateStatus


class _CRM:
    def __init__(self, stage):
        self.stage = stage
        self.moved: list[tuple[int, int]] = []

    async def live_card_status(self, lead_id):
        return self.stage

    async def move_to_status(self, lead_id, status_id):
        self.moved.append((lead_id, status_id))
        self.stage = status_id

    async def aclose(self):
        pass


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session
            await session.commit()

    monkeypatch.setattr(tg_gate, "session_scope", scope)
    monkeypatch.setattr(tg_silence, "session_scope", scope)
    yield maker
    await engine.dispose()


async def _seed(maker, **kw) -> None:
    defaults = dict(full_name="Гафінець Сергій", phone_e164="+380671234567",
                    source="workua_response_send", status=CandidateStatus.MANAGER_REVIEW.value,
                    keycrm_lead_id=11618)
    defaults.update(kw)
    async with maker() as session:
        session.add(Candidate(**defaults))
        await session.commit()


async def _status(maker) -> str:
    async with maker() as session:
        return (await session.execute(select(Candidate))).scalar_one().status


async def _call(action, crm):
    return await handle_tg_silence(peer_id="8956121812", phone="+380671234567",
                                   action=action, crm=crm)


async def test_a_chat_in_work_gets_the_reminder(db):
    await _seed(db)
    assert (await _call("check", _CRM(3)))["eligible"] is True


async def test_a_card_elsewhere_is_left_alone(db):
    await _seed(db)
    crm = _CRM(2)

    assert (await _call("check", crm))["eligible"] is False
    assert (await _call("close", crm))["closed"] is False
    assert crm.moved == []


async def test_a_promised_callback_belongs_to_the_dialer(db):
    await _seed(db, status=CandidateStatus.IN_CALL_QUEUE.value,
                callback_at=datetime.utcnow() + timedelta(hours=2))
    assert (await _call("check", _CRM(3)))["eligible"] is False


async def test_silence_after_the_reminder_closes_the_card(db):
    await _seed(db)
    crm = _CRM(3)

    res = await _call("close", crm)

    assert res["closed"] is True
    assert crm.moved == [(11618, 32)]
    assert await _status(db) == "closed"


async def test_writing_back_reopens_the_chat(db):
    await _seed(db, status=CandidateStatus.CLOSED.value)
    crm = _CRM(32)

    res = await _call("reopen", crm)

    assert res["reopened"] is True
    assert crm.moved == [(11618, 3)]
    assert await _status(db) == "manager_review"


async def test_a_recruiters_verdict_is_not_reopened(db):
    await _seed(db, status=CandidateStatus.CLOSED.value)
    crm = _CRM(34)

    assert (await _call("reopen", crm))["reopened"] is False
    assert crm.moved == []


async def test_no_card_nothing_to_do(db):
    assert (await _call("check", _CRM(3)))["eligible"] is False
