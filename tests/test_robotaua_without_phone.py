"""A real robota.ua application without a visible phone still gets a card.

The client (28.09): every candidate who applies must be in CRM, with a phone or
without. A CV file without a number, or contacts robota.ua hides until a paid
opening, left the person parked out of sight — Artem, Karina Dorohii and six
more that day. `Interaction` rows stay out: they are views and recommendations,
not applications (the 04.09 incident).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api.inbound_router import IngestPayload, IngestResult, InboundRouter
from src.common.models import Base, Candidate
from src.integrations import robotaua_sync as rs

SALES_ID = 11277559


def _apply(apply_id, name, *, kind="AttachedFile", phone=""):
    return {"id": apply_id, "resumeId": 0 if kind == "AttachedFile" else apply_id + 1000,
            "name": name, "vacancyId": SALES_ID, "resumeType": kind, "phone": phone,
            "addDate": (datetime.utcnow() - timedelta(minutes=5)).isoformat(timespec="seconds")}


class _Cabinet:
    def __init__(self, applies):
        self.applies = applies

    async def city_map(self):
        return {}

    async def list_applies(self, *, page=0, count=50):
        return list(self.applies) if page == 0 else []

    async def get_resume(self, resume_id):
        return {}

    async def open_contacts_count(self):
        return {"availableContacts": 0}

    async def download_attachment(self, apply_id, url=None):
        return b""


class _Router:
    def __init__(self):
        self.payloads: list[IngestPayload] = []

    async def ingest(self, payload):
        self.payloads.append(payload)
        return IngestResult(accepted=True, candidate_id=len(self.payloads))


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOTAUA_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ROBOTAUA_ALLOWED_VACANCY_IDS", raising=False)
    monkeypatch.delenv("ROBOTAUA_DRY_RUN", raising=False)
    monkeypatch.setattr(rs, "REQUEST_PAUSE_SEC", 0)


async def test_hidden_phone_application_gets_a_card_now():
    router = _Router()

    await rs.poll_responses(client=_Cabinet([_apply(7, "Artem")]), router=router)

    assert [p.full_name for p in router.payloads] == ["Artem"]
    assert router.payloads[0].no_phone_key == "rua7"
    assert rs.load_cursor()["pending"] == {}


async def test_an_interaction_without_phone_still_stays_out():
    router = _Router()

    await rs.poll_responses(client=_Cabinet([_apply(8, "Переглянув", kind="Interaction")]),
                            router=router)

    assert router.payloads == []


async def test_a_real_application_parked_earlier_gets_its_card_on_the_next_poll():
    rs.save_cursor({"pending": {
        "9": {"resume_id": 0, "name": "Karina Dorohii", "vacancy_id": SALES_ID,
              "resume_type": "AttachedFile"},
        "10": {"resume_id": 1010, "name": "Elina Elina", "vacancy_id": SALES_ID,
               "resume_type": "Interaction"},
    }})
    router = _Router()

    await rs.poll_responses(client=_Cabinet([]), router=router)

    assert [p.full_name for p in router.payloads] == ["Karina Dorohii"]
    assert set(rs.load_cursor()["pending"]) == {"10"}


async def test_a_parked_application_keeps_its_email_on_the_card():
    # 29.09: the parked queue kept the name but not the email, so the six cards
    # built from it on 28.09 had no way to reach the person at all.
    apply = {"id": 11, "name": "Karina Dorohii", "vacancyId": SALES_ID, "resumeId": 0,
             "resumeType": "AttachedFile", "phone": "", "eMail": "karina@example.com"}
    rs.save_cursor({"pending": {"11": rs._parked_entry(apply)}})
    router = _Router()

    await rs.poll_responses(client=_Cabinet([]), router=router)

    assert [p.email for p in router.payloads] == ["karina@example.com"]


# ---- the intake side: a card with no phone ----------------------------------

class _CRM:
    def __init__(self):
        self.created: list[dict] = []

    async def create_lead(self, **kw):
        self.created.append(kw)
        return {"id": 700 + len(self.created)}

    async def card_pipeline(self, lead_id):
        return 1


def _autocommitting(maker):
    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session
            await session.commit()
    return scope


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.api.inbound_router.session_scope", _autocommitting(maker),
                        raising=False)
    yield maker
    await engine.dispose()


def _hidden(**kw):
    defaults = dict(full_name="Artem", phone_raw="", no_phone_key="rua7",
                    source="robotaua_response", vacancy_key="sales", is_response=True)
    defaults.update(kw)
    return IngestPayload(**defaults)


async def test_intake_makes_a_card_without_a_phone(db):
    crm = _CRM()

    result = await InboundRouter(keycrm=crm).ingest(_hidden())

    assert result.accepted and not result.duplicate
    assert crm.created[0]["phone"] is None
    assert "телефон прихований" in crm.created[0]["manager_comment"]
    async with db() as session:
        row = (await session.execute(select(Candidate))).scalar_one()
    assert row.phone_e164 == "rua7"


async def test_the_same_hidden_application_twice_is_one_card(db):
    crm = _CRM()
    router = InboundRouter(keycrm=crm)

    await router.ingest(_hidden())
    second = await router.ingest(_hidden())

    assert second.duplicate is True
    assert len(crm.created) == 1
