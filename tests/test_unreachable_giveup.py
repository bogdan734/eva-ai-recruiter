"""«Недозвін» drains even when one of its cards is gone from KeyCRM.

The daily give-up moved every no-answer candidate older than three days to «Не
актуально» in one transaction. Recruiters had deleted some of those cards by
hand, KeyCRM answered 404 for the first of them, and the job died on it -- the
whole batch rolled back, every morning. On 29.09 fifteen cards had sat in the
column since 18.09 and the job had never moved one.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.common.models import Base, Candidate, CandidateStatus
from src.scheduler import dispatcher


class _CRM:
    def __init__(self, gone: set[int]):
        self.gone = gone
        self.moved: list[tuple[int, int]] = []

    async def move_to_status(self, lead_id, status_id):
        if lead_id in self.gone:
            request = httpx.Request("PUT", f"https://openapi.keycrm.app/v1/pipelines/cards/{lead_id}")
            raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
        self.moved.append((lead_id, status_id))

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

    monkeypatch.setattr(dispatcher, "session_scope", scope)
    yield maker
    await engine.dispose()


async def _unreachable(maker, lead_id: int, phone: str, days_ago: int) -> None:
    async with maker() as session:
        session.add(Candidate(
            full_name=f"lead {lead_id}", phone_e164=phone, keycrm_lead_id=lead_id,
            status=CandidateStatus.UNREACHABLE.value,
            updated_at=datetime.utcnow() - timedelta(days=days_ago),
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_a_deleted_card_does_not_stop_the_others(db, monkeypatch):
    crm = _CRM(gone={10565})
    monkeypatch.setattr("src.common.crm.get_crm", lambda: crm)
    await _unreachable(db, 10565, "+380500000001", days_ago=40)
    await _unreachable(db, 11459, "+380500000002", days_ago=9)

    await dispatcher.disposition_stale_unreachable()

    assert crm.moved == [(11459, 32)]
    async with db() as session:
        statuses = {c.keycrm_lead_id: c.status for c in (await session.execute(select(Candidate))).scalars()}
    assert statuses == {10565: "closed", 11459: "closed"}


@pytest.mark.asyncio
async def test_recent_no_answers_wait_their_three_days(db, monkeypatch):
    crm = _CRM(gone=set())
    monkeypatch.setattr("src.common.crm.get_crm", lambda: crm)
    await _unreachable(db, 11606, "+380500000003", days_ago=1)

    await dispatcher.disposition_stale_unreachable()

    assert crm.moved == []
