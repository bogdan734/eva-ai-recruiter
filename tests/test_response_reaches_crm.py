"""A response is a card. Two ways InboundRouter.ingest() let one go without.

1. DEFER_KEYCRM_UNTIL_QUALIFIED holds a card back until Єва has called the
   person. Since 04.09 Єва never calls anyone who applied themselves, so for a
   response "deferred" means "never". The code default is on, and so is
   .env.example: the card depended on one line of the live .env staying off.

2. A repeat response on a vacancy Єва works returned `local_duplicate` whether
   or not the person had a card. Anyone we held without one — ingested while
   deferral was on, found by cold sourcing and still waiting for a call, or
   whose card creation failed — answered the posting again and vanished.

And one way it never retried: a failed create is `keycrm_failed`, counted as
accepted, and the board cursor moves past the applicant. `issue_missing_cards`
is the retry.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api.inbound_router import InboundRouter, IngestPayload, issue_missing_cards
from src.common import vacancies
from src.common.models import Base, Candidate, CandidateStatus

PHONE = "+380671112233"


def _autocommitting(maker):
    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session
            await session.commit()
    return scope


class _FakeCRM:
    def __init__(self, *, pipelines: dict[int, int | None] | None = None, lookup_fails=False):
        self.created: list[dict] = []
        self.comments: list[tuple[int, str]] = []
        self.moves: list[tuple[int, int]] = []
        self._pipelines = pipelines or {}
        self._lookup_fails = lookup_fails

    async def create_lead(self, **kw):
        self.created.append(kw)
        return {"id": 9000 + len(self.created)}

    async def card_pipeline(self, lead_id):
        if self._lookup_fails:
            raise RuntimeError("KeyCRM unreachable")
        return self._pipelines.get(lead_id)

    async def append_manager_comment(self, lead_id, addition):
        self.comments.append((lead_id, addition))
        return {}

    async def move_to_status(self, lead_id, status_id):
        self.moves.append((lead_id, status_id))
        return {}


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.api.inbound_router.session_scope", _autocommitting(maker))
    # The live default. Every test here must hold with deferral switched on.
    monkeypatch.setenv("DEFER_KEYCRM_UNTIL_QUALIFIED", "1")
    yield maker
    await engine.dispose()


async def _seed(maker, **kw) -> int:
    row = dict(
        full_name="Сулім Олександр", phone_e164=PHONE, source="workua_search",
        status=CandidateStatus.NEW_RESUME, vacancy_id=vacancies.LOCAL_FK,
        vacancy_key="sales", keycrm_lead_id=None,
    )
    row.update(kw)
    async with maker() as session:
        c = Candidate(**row)
        session.add(c)
        await session.commit()
        await session.refresh(c)
        return c.id


def _response(**kw) -> IngestPayload:
    p = dict(
        full_name="Сулім Олександр", phone_raw=PHONE, source="workua_response_send",
        vacancy_key="sales", board_vacancy_id=8249916, is_response=True,
    )
    p.update(kw)
    return IngestPayload(**p)


async def _lead_id(maker, cid: int) -> int | None:
    async with maker() as session:
        return (await session.get(Candidate, cid)).keycrm_lead_id


@pytest.mark.asyncio
async def test_response_to_a_called_vacancy_gets_a_card_with_deferral_on(db):
    crm = _FakeCRM()

    result = await InboundRouter(keycrm=crm).ingest(_response())

    assert len(crm.created) == 1
    assert crm.created[0]["pipeline_id"] == vacancies.get("sales").keycrm_pipeline_id
    assert result.keycrm_lead_id == 9001
    assert await _lead_id(db, result.candidate_id) == 9001


@pytest.mark.asyncio
async def test_sourced_candidate_is_still_deferred_until_the_call(db, monkeypatch):
    async def _slavic(_name):
        return True

    monkeypatch.setattr("src.api.inbound_router.is_slavic_name", _slavic)
    crm = _FakeCRM()

    result = await InboundRouter(keycrm=crm).ingest(
        _response(source="workua_search", is_response=False)
    )

    assert crm.created == []
    assert result.reason == "deferred_until_qualified"


@pytest.mark.asyncio
async def test_repeat_response_from_someone_held_without_a_card_gets_one(db):
    cid = await _seed(db)
    crm = _FakeCRM()

    result = await InboundRouter(keycrm=crm).ingest(_response())

    assert len(crm.created) == 1
    assert result.duplicate is False
    assert result.candidate_id == cid
    assert await _lead_id(db, cid) == 9001


@pytest.mark.asyncio
async def test_repeat_response_whose_card_was_deleted_gets_a_new_one(db):
    cid = await _seed(db, keycrm_lead_id=555, status=CandidateStatus.MANAGER_REVIEW)
    crm = _FakeCRM(pipelines={555: None})

    await InboundRouter(keycrm=crm).ingest(_response())

    assert len(crm.created) == 1
    assert await _lead_id(db, cid) == 9001


@pytest.mark.asyncio
async def test_repeat_response_with_a_live_card_only_annotates_it(db):
    cid = await _seed(db, keycrm_lead_id=555, status=CandidateStatus.MANAGER_REVIEW)
    # Moved on to a later funnel by the recruiter: still a live card, not ours to redo.
    crm = _FakeCRM(pipelines={555: 2})

    result = await InboundRouter(keycrm=crm).ingest(_response())

    assert crm.created == []
    assert result.duplicate is True
    assert [lead for lead, _ in crm.comments] == [555]
    assert await _lead_id(db, cid) == 555


@pytest.mark.asyncio
async def test_unreachable_crm_never_reads_as_a_deleted_card(db):
    await _seed(db, keycrm_lead_id=555, status=CandidateStatus.MANAGER_REVIEW)
    crm = _FakeCRM(lookup_fails=True)

    result = await InboundRouter(keycrm=crm).ingest(_response())

    assert crm.created == []
    assert result.duplicate is True


class _FlakyCRM(_FakeCRM):
    """KeyCRM answering 429 to the first create, as it does under a burst."""

    def __init__(self):
        super().__init__()
        self.fail_next = True

    async def create_lead(self, **kw):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("429 Too Many Requests")
        return await super().create_lead(**kw)


@pytest.mark.asyncio
async def test_a_failed_create_is_retried_by_the_sweep(db):
    """`keycrm_failed` counts as accepted and the board cursor moves on; before
    the sweep nothing ever asked KeyCRM again."""
    crm = _FlakyCRM()
    router = InboundRouter(keycrm=crm)

    first = await router.ingest(_response(vacancy_key="accountant", board_vacancy_id=8242731))
    assert first.reason.startswith("keycrm_failed")
    assert await _lead_id(db, first.candidate_id) is None

    # Fresh rows are left alone for a while: the ingest may still be talking to KeyCRM.
    assert await issue_missing_cards(router) == 0

    made = await issue_missing_cards(router, settle_minutes=0)

    assert made == 1
    assert len(crm.created) == 1
    assert crm.created[0]["pipeline_id"] == vacancies.get("accountant").keycrm_pipeline_id
    assert await _lead_id(db, first.candidate_id) == 9001
    assert await issue_missing_cards(router, settle_minutes=0) == 0


@pytest.mark.asyncio
async def test_the_sweep_leaves_rows_that_are_not_waiting_for_a_human(db):
    await _seed(db, status=CandidateStatus.NEW_RESUME)  # sourced, deferred until the call
    await _seed(db, phone_e164="tg555", source="telegram",
                status=CandidateStatus.MANAGER_REVIEW)  # no number to put on a card
    await _seed(db, phone_e164="+380501112233", status=CandidateStatus.MANAGER_REVIEW,
                created_at=datetime.now(timezone.utc) - timedelta(days=30))  # long settled
    crm = _FakeCRM()

    assert await issue_missing_cards(InboundRouter(keycrm=crm), settle_minutes=0) == 0
    assert crm.created == []


@pytest.mark.asyncio
async def test_a_given_up_candidate_with_a_new_card_is_back_in_review(db):
    """The unreachable sweep would otherwise close the fresh card again."""
    cid = await _seed(db, status=CandidateStatus.UNREACHABLE)
    crm = _FakeCRM()

    await InboundRouter(keycrm=crm).ingest(_response())

    async with db() as session:
        assert (await session.get(Candidate, cid)).status == CandidateStatus.MANAGER_REVIEW
