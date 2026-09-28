"""InboundRouter.ingest()'s repeat-application path (src/api/inbound_router.py).

07.09.2026: a repeat robota.ua/work.ua response used to vanish in silence; it
became a comment on the existing card. 28.09.2026, the client: a repeat
application must show up in CRM like any other — its own new card in «Новий»,
marked with the previous card's number, while the old card gets a pointer to
the new one. The one exception is the same person applying to two postings
within a day (seen two minutes apart): that is one application, noted on the
card it already made.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api.inbound_router import IngestPayload, InboundRouter
from src.common import vacancies
from src.common.models import Base, Candidate, CandidateStatus


def _autocommitting(maker):
    """Wrap a bare async_sessionmaker so it commits on a clean exit, matching
    the real `session_scope()` (src/common/db.py) instead of AsyncSession's own
    __aexit__, which only closes. Without this, a status change made inside
    `async with session_scope() as session:` in the code under test -- exactly
    how InboundRouter.ingest() uses it -- would silently vanish in tests while
    working correctly in production."""
    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session
            await session.commit()
    return scope


class _FakeCRM:
    def __init__(self):
        self.comments: list[tuple[int, str]] = []
        self.moves: list[tuple[int, int]] = []
        self.created: list[dict] = []

    async def card_pipeline(self, lead_id):
        return 1  # the card is alive

    async def card_age(self, lead_id):
        return self.age

    age = (1, datetime.now(UTC) - timedelta(days=3))  # (pipeline, created_at)

    async def create_lead(self, **kw):
        self.created.append(kw)
        return {"id": 777}

    async def append_manager_comment(self, lead_id, addition):
        self.comments.append((lead_id, addition))
        return {}

    async def move_to_status(self, lead_id, status_id):
        self.moves.append((lead_id, status_id))
        return {}


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed(maker, **kw) -> int:
    defaults = dict(
        full_name="Таран Максим", phone_e164="+380991112233",
        source="robotaua_response", status=CandidateStatus.CLOSED,
        vacancy_id=vacancies.LOCAL_FK, vacancy_key="sales",
        keycrm_lead_id=555,
    )
    defaults.update(kw)
    async with maker() as session:
        c = Candidate(**defaults)
        session.add(c)
        await session.commit()
        await session.refresh(c)
        return c.id


def _payload(**kw) -> IngestPayload:
    defaults = dict(
        full_name="Таран Максим", phone_raw="+380991112233",
        source="robotaua_response", vacancy_key="sales", is_response=True,
    )
    defaults.update(kw)
    return IngestPayload(**defaults)


@pytest.mark.asyncio
async def test_repeat_application_gets_its_own_new_card(db, monkeypatch):
    monkeypatch.setattr("src.api.inbound_router.session_scope", _autocommitting(db), raising=False)
    cid = await _seed(db, status=CandidateStatus.MANAGER_REVIEW)
    crm = _FakeCRM()
    router = InboundRouter(keycrm=crm)

    result = await router.ingest(_payload())

    assert result.duplicate is False
    assert len(crm.created) == 1
    assert "попередня картка #555" in crm.created[0]["manager_comment"]
    assert result.keycrm_lead_id == 777
    async with db() as session:
        assert (await session.get(Candidate, cid)).keycrm_lead_id == 777


@pytest.mark.asyncio
async def test_old_card_points_to_the_new_one_and_stays_put(db, monkeypatch):
    monkeypatch.setattr("src.api.inbound_router.session_scope", _autocommitting(db), raising=False)
    await _seed(db, status=CandidateStatus.INTERVIEW_SCHEDULED)
    crm = _FakeCRM()
    router = InboundRouter(keycrm=crm)

    await router.ingest(_payload())

    assert [c for c in crm.comments if c[0] == 555]
    assert "#777" in [c for c in crm.comments if c[0] == 555][0][1]
    assert crm.moves == []


@pytest.mark.asyncio
async def test_closed_or_unreachable_person_applying_again_gets_a_new_card(db, monkeypatch):
    monkeypatch.setattr("src.api.inbound_router.session_scope", _autocommitting(db), raising=False)
    for status, phone in ((CandidateStatus.CLOSED, "+380991112233"),
                          (CandidateStatus.UNREACHABLE, "+380991112244")):
        cid = await _seed(db, status=status, phone_e164=phone)
        crm = _FakeCRM()
        router = InboundRouter(keycrm=crm)

        await router.ingest(_payload(phone_raw=phone))

        assert len(crm.created) == 1, status
        async with db() as session:
            assert (await session.get(Candidate, cid)).status == CandidateStatus.MANAGER_REVIEW


@pytest.mark.asyncio
async def test_second_posting_the_same_day_is_one_application(db, monkeypatch):
    monkeypatch.setattr("src.api.inbound_router.session_scope", _autocommitting(db), raising=False)
    await _seed(db, status=CandidateStatus.MANAGER_REVIEW)
    crm = _FakeCRM()
    crm.age = (1, datetime.now(UTC) - timedelta(minutes=2))
    router = InboundRouter(keycrm=crm)

    result = await router.ingest(_payload())

    assert result.duplicate is True
    assert crm.created == []
    assert crm.comments and "повторний відгук" in crm.comments[0][1]


@pytest.mark.asyncio
async def test_sourced_rediscovery_is_silent_not_a_response(db, monkeypatch):
    """A cold-sourcing re-find of the same phone is not the candidate doing
    anything -- no comment, no reactivation, matching pre-existing behaviour."""
    monkeypatch.setattr("src.api.inbound_router.session_scope", _autocommitting(db), raising=False)
    await _seed(db, status=CandidateStatus.CLOSED)
    crm = _FakeCRM()
    router = InboundRouter(keycrm=crm)

    await router.ingest(_payload(is_response=False, source="workua_search"))

    assert crm.comments == []
    assert crm.moves == []


@pytest.mark.asyncio
async def test_no_existing_card_gets_one_instead_of_nothing(db, monkeypatch):
    """A closed candidate can have no keycrm_lead_id (deferred mode, a failed
    create). This used to be a silent no-op, which is how a repeat applicant
    never reached the CRM at all. Now the response makes the card; there is
    nothing to comment on or move."""
    monkeypatch.setattr("src.api.inbound_router.session_scope", _autocommitting(db), raising=False)
    await _seed(db, status=CandidateStatus.CLOSED, keycrm_lead_id=None)
    crm = _FakeCRM()
    router = InboundRouter(keycrm=crm)

    result = await router.ingest(_payload())

    assert result.duplicate is False
    assert len(crm.created) == 1
    assert crm.comments == []
    assert crm.moves == []


@pytest.mark.asyncio
async def test_a_first_time_applicant_is_unaffected(db, monkeypatch):
    """No existing row at all -- the new-candidate path, completely untouched
    by this change."""
    monkeypatch.setattr("src.api.inbound_router.session_scope", _autocommitting(db), raising=False)
    crm = _FakeCRM()
    router = InboundRouter(keycrm=crm)

    result = await router.ingest(_payload(phone_raw="+380997778899"))

    assert result.duplicate is False
    assert crm.comments == []
    assert crm.moves == []
