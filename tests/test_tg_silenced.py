"""Eva stays quiet when a recruiter owns the card — the recruiters must hear why.

Третьяк Руслана answered the outreach on 26.09 («Так, ще цікавить»); the gate
kept Eva silent because the card was with a recruiter, and nobody learned she
had written. The API now hands such a reply to the admins and notes it on the
card.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api import tg_silenced
from src.api.tg_silenced import alert_text, handle_tg_silenced
from src.common import vacancies
from src.common.models import Base, Candidate, CandidateStatus


def _autocommitting(maker):
    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session
            await session.commit()
    return scope


class _CRM:
    def __init__(self, pipeline=1):
        self.pipeline = pipeline
        self.comments: list[tuple[int, str]] = []

    async def card_pipeline(self, lead_id):
        if isinstance(self.pipeline, Exception):
            raise self.pipeline
        return self.pipeline

    async def append_manager_comment(self, lead_id, addition):
        self.comments.append((lead_id, addition))
        return {}

    async def aclose(self):
        pass


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tg_silenced, "session_scope", _autocommitting(maker))
    yield maker
    await engine.dispose()


@pytest.fixture
def alerts(monkeypatch):
    sent: list[str] = []

    def install(delivered: int):
        async def fake(text):
            sent.append(text)
            return delivered
        monkeypatch.setattr(tg_silenced, "alert_admins", fake)
        return sent
    return install


async def _seed(maker, **kw):
    defaults = dict(
        full_name="Третьяк Руслана Вадимівна", phone_e164="+380671234567",
        source="workua_response_send", status=CandidateStatus.MANAGER_REVIEW,
        vacancy_id=vacancies.LOCAL_FK, vacancy_key="sales", keycrm_lead_id=11577,
    )
    defaults.update(kw)
    async with maker() as session:
        session.add(Candidate(**defaults))
        await session.commit()


def test_alert_names_the_person_the_card_and_what_they_said():
    text = alert_text(name="Третьяк Руслана Вадимівна", username=None, phone="+380671234567",
                      status="manager_review", lead_id=11577, card="alive",
                      messages=["Так, ще цікавить"])

    assert "Третьяк Руслана Вадимівна" in text
    assert "+380671234567" in text
    assert "#11577" in text
    assert "«Так, ще цікавить»" in text


def test_alert_says_when_the_card_was_deleted_in_crm():
    text = alert_text(name="Озерний Микита", username=None, phone=None,
                      status="manager_review", lead_id=10545, card="deleted",
                      messages=["Актуально"])

    assert "#10545" in text
    assert "видалено" in text


def test_candidate_text_cannot_break_the_html_message():
    text = alert_text(name="<b>x</b>", username=None, phone=None, status=None,
                      lead_id=None, card=None, messages=["ціна <5000 & більше"])

    assert "<b>x</b>" not in text
    assert "&lt;5000 &amp; більше" in text


async def test_reply_reaches_the_admins_and_the_card(db, alerts):
    await _seed(db)
    sent = alerts(2)
    crm = _CRM()

    res = await handle_tg_silenced(peer_id="528590644", name="Руслана", username=None,
                                   phone="+380671234567", messages=["Так, ще цікавить"],
                                   crm=crm)

    assert res == {"ok": True, "delivered": 2}
    assert "#11577" in sent[0]
    assert crm.comments and crm.comments[0][0] == 11577
    assert "Так, ще цікавить" in crm.comments[0][1]


async def test_hidden_number_is_found_by_the_peer_surrogate(db, alerts):
    await _seed(db, full_name="Бричка Анна", phone_e164="tg940825891", keycrm_lead_id=None)
    sent = alerts(1)

    res = await handle_tg_silenced(peer_id="940825891", name="", username=None,
                                   phone=None, messages=["Так актуально"], crm=_CRM())

    assert res["ok"] is True
    assert "Бричка Анна" in sent[0]


async def test_nobody_heard_it_is_not_ok(db, alerts):
    await _seed(db)
    alerts(0)

    res = await handle_tg_silenced(peer_id="528590644", name="", username=None,
                                   phone="+380671234567", messages=["Так"], crm=_CRM())

    assert res["ok"] is False


async def test_deleted_card_gets_no_comment_but_the_alert_still_goes(db, alerts):
    await _seed(db)
    sent = alerts(1)
    crm = _CRM(pipeline=None)

    res = await handle_tg_silenced(peer_id="528590644", name="", username=None,
                                   phone="+380671234567", messages=["Так"], crm=crm)

    assert res["ok"] is True
    assert crm.comments == []
    assert "видалено" in sent[0]


async def test_crm_outage_does_not_stop_the_alert(db, alerts):
    await _seed(db)
    sent = alerts(1)
    crm = _CRM(pipeline=RuntimeError("keycrm 503"))

    res = await handle_tg_silenced(peer_id="528590644", name="", username=None,
                                   phone="+380671234567", messages=["Так"], crm=crm)

    assert res["ok"] is True
    assert sent


async def test_route_needs_the_internal_token(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from src.api.main import app
    from src.common.settings import get_settings

    async def fake_handle(**kw):
        return {"ok": True, "delivered": 1, "seen": kw["messages"]}
    monkeypatch.setattr(tg_silenced, "handle_tg_silenced", fake_handle)

    body = {"peer_id": "528590644", "messages": ["Так, ще цікавить"]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        denied = await c.post("/internal/tg-silenced", json=body)
        allowed = await c.post("/internal/tg-silenced", json=body,
                               headers={"X-Internal-Token": get_settings().internal_api_token})

    assert denied.status_code == 401
    assert allowed.json()["seen"] == ["Так, ще цікавить"]
