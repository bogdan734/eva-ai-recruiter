"""A candidate a recruiter owns writes to Eva — the recruiter must hear it.

Eva leads Telegram dialogs herself until a recruiter has the card; from then on
she stays quiet and the message goes to the recruiters' bot chat and onto the
card. A deleted or dispositioned card pings nobody: the first is Eva's to lead,
the second has been decided.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api import tg_gate, tg_silenced
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
    def __init__(self, stage=2, error=None):
        self.stage, self.error = stage, error
        self.comments: list[tuple[int, str]] = []

    async def live_card_status(self, lead_id):
        if self.error:
            raise self.error
        return self.stage

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
    monkeypatch.setattr(tg_gate, "session_scope", _autocommitting(maker))
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
        full_name="Фітьо Уляна", phone_e164="+380671234567",
        source="workua_response_send", status=CandidateStatus.MANAGER_REVIEW,
        vacancy_id=vacancies.LOCAL_FK, vacancy_key="sales", keycrm_lead_id=11398,
    )
    defaults.update(kw)
    async with maker() as session:
        session.add(Candidate(**defaults))
        await session.commit()


def test_alert_names_the_person_the_card_its_stage_and_the_message():
    text = alert_text(name="Фітьо Уляна", username=None, phone="+380671234567",
                      lead_id=11398, stage=2, messages=["Коли зі мною зв'яжуться?"])

    assert "Фітьо Уляна" in text
    assert "+380671234567" in text
    assert "#11398" in text
    assert "«Відібрано»" in text
    assert "«Коли зі мною зв'яжуться?»" in text


def test_candidate_text_cannot_break_the_html_message():
    text = alert_text(name="<b>x</b>", username=None, phone=None, lead_id=1, stage=3,
                      messages=["ціна <5000 & більше"])

    assert "<b>x</b>" not in text
    assert "&lt;5000 &amp; більше" in text


async def test_recruiters_card_gets_the_alert_and_a_comment(db, alerts):
    await _seed(db)
    sent = alerts(2)
    crm = _CRM(stage=2)

    res = await handle_tg_silenced(peer_id="1142038202", name="", username=None,
                                   phone="+380671234567",
                                   messages=["Коли зі мною зв'яжуться?"], crm=crm)

    assert res == {"ok": True, "delivered": 2}
    assert "#11398" in sent[0]
    assert crm.comments and crm.comments[0][0] == 11398


async def test_rejected_card_pings_nobody(db, alerts):
    await _seed(db, full_name="Бричка Анна", phone_e164="+380501112233", keycrm_lead_id=11228)
    sent = alerts(2)

    res = await handle_tg_silenced(peer_id="940825891", name="", username=None,
                                   phone="+380501112233", messages=["Вже не актуально"],
                                   crm=_CRM(stage=34))

    assert res["ok"] is True and res["skipped"] == "final_stage"
    assert sent == []


async def test_deleted_card_pings_nobody_because_eva_leads_it(db, alerts):
    await _seed(db, keycrm_lead_id=10545)
    sent = alerts(2)

    res = await handle_tg_silenced(peer_id="8337431419", name="", username=None,
                                   phone="+380671234567", messages=["Актуально"],
                                   crm=_CRM(stage=None))

    assert res["skipped"] == "card_deleted"
    assert sent == []


async def test_hidden_number_is_found_by_the_peer_surrogate(db, alerts):
    await _seed(db, full_name="Бричка Анна", phone_e164="tg940825891")
    sent = alerts(1)

    res = await handle_tg_silenced(peer_id="940825891", name="", username=None,
                                   phone=None, messages=["Так"], crm=_CRM(stage=4))

    assert res["ok"] is True
    assert "Бричка Анна" in sent[0]


async def test_nobody_heard_it_is_not_ok(db, alerts):
    await _seed(db)
    alerts(0)

    res = await handle_tg_silenced(peer_id="1142038202", name="", username=None,
                                   phone="+380671234567", messages=["Так"], crm=_CRM(stage=2))

    assert res["ok"] is False


async def test_crm_outage_still_reaches_the_recruiters(db, alerts):
    await _seed(db)
    sent = alerts(1)

    res = await handle_tg_silenced(peer_id="1142038202", name="", username=None,
                                   phone="+380671234567", messages=["Так"],
                                   crm=_CRM(error=RuntimeError("keycrm 503")))

    assert res["ok"] is True
    assert "не вдалося перевірити" in sent[0]


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
