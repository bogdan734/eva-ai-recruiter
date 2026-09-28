"""A Telegram dialog Eva leads is visible in CRM, and a qualified one reaches the bot.

Recruiters delete cards in KeyCRM (the whole 18–19.08 batch is gone). Writing
the transcript to a deleted card failed on every message, so Eva's work with
those people was invisible. A deleted card is now replaced once the dialog is
real, and a candidate who fully qualifies is announced in the recruiters' bot.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api import tg_outcome
from src.api.tg_outcome import candidate_turns, handle_tg_outcome, handle_tg_progress
from src.common import vacancies
from src.common.models import Base, Candidate, CandidateStatus

DIALOG = ("Єва: Доброго дня! Перепрошую за паузу…\n"
          "Кандидат: Так, актуально\n"
          "Єва: Підкажіть, у якому місті ви проживаєте?\n"
          "Кандидат: Львів, 29 років")


class _CRM:
    def __init__(self, alive=True, live_stage=3):
        self.alive, self.live_stage = alive, live_stage
        self.created: list[dict] = []
        self.transcripts: list[int] = []
        self.moves: list[tuple[int, int]] = []

    async def card_pipeline(self, lead_id):
        return 1 if self.alive else None

    async def create_lead(self, **kw):
        self.created.append(kw)
        return {"id": 20001}

    async def write_call_results(self, lead_id, **kw):
        self.transcripts.append(lead_id)

    async def assign_manager(self, lead_id, manager_id):
        pass

    async def move_to_status(self, lead_id, status_id):
        self.moves.append((lead_id, status_id))

    async def get_card_status(self, lead_id):
        return self.live_stage

    async def find_buyer_by_phone(self, phone):
        return None

    async def aclose(self):
        pass


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
    monkeypatch.setattr(tg_outcome, "session_scope", _autocommitting(maker))
    yield maker
    await engine.dispose()


@pytest.fixture
def crm(monkeypatch):
    def install(**kw):
        fake = _CRM(**kw)
        monkeypatch.setattr(tg_outcome, "get_crm", lambda: fake)
        return fake
    return install


@pytest.fixture
def alerts(monkeypatch):
    sent: list[str] = []

    async def fake(text):
        sent.append(text)
        return 1
    monkeypatch.setattr(tg_outcome, "alert_admins", fake)
    return sent


async def _seed(maker, **kw) -> int:
    defaults = dict(
        full_name="Гриценко Анастасія", phone_e164="+380671234567",
        source="workua_response_send", status=CandidateStatus.MANAGER_REVIEW,
        vacancy_id=vacancies.LOCAL_FK, vacancy_key="sales", keycrm_lead_id=10559,
    )
    defaults.update(kw)
    async with maker() as session:
        c = Candidate(**defaults)
        session.add(c)
        await session.commit()
        return c.id


async def _lead_of(maker, cid):
    async with maker() as session:
        return (await session.execute(
            select(Candidate.keycrm_lead_id).where(Candidate.id == cid))).scalar_one()


def test_candidate_turns_are_counted_in_the_userbots_format():
    assert candidate_turns(DIALOG) == 2
    assert candidate_turns("[Кандидат] так\n[Кандидат] Львів") == 2


async def test_deleted_card_is_replaced_once_the_dialog_is_real(db, crm):
    cid = await _seed(db)
    fake = crm(alive=False)

    res = await handle_tg_progress(peer_id="915078090", name="", username=None,
                                   phone="+380671234567", transcript=DIALOG)

    assert res["created"] is True
    assert fake.created and "#10559" in fake.created[0]["manager_comment"]
    # Eva is still talking: «В роботі», not a selection (client's rule 02.09)
    assert fake.created[0]["status_id"] == 3 and fake.moves[-1] == (20001, 3)
    assert await _lead_of(db, cid) == 20001


async def test_greeting_alone_does_not_make_a_card(db, crm):
    await _seed(db)
    fake = crm(alive=False)

    res = await handle_tg_progress(peer_id="915078090", name="", username=None,
                                   phone="+380671234567",
                                   transcript="Єва: Доброго дня!\nКандидат: Доброго дня!")

    assert res.get("skipped") == "dialog_too_short"
    assert fake.created == []


async def test_live_card_gets_the_transcript(db, crm):
    await _seed(db)
    fake = crm(alive=True)

    await handle_tg_progress(peer_id="915078090", name="", username=None,
                             phone="+380671234567", transcript=DIALOG)

    assert fake.transcripts == [10559]
    assert fake.created == []


async def test_qualified_candidate_goes_to_the_recruiters_bot(db, crm, alerts):
    cid = await _seed(db)
    fake = crm(alive=False)

    res = await handle_tg_outcome(peer_id="915078090", name="", username=None,
                                  phone="+380671234567", verdict="qualified",
                                  region="Львівська", age=29,
                                  summary="- досвід продажів 2 роки", transcript=DIALOG)

    assert res["ok"] is True
    assert await _lead_of(db, cid) == 20001
    assert len(alerts) == 1
    assert "Гриценко Анастасія" in alerts[0] and "#20001" in alerts[0]
    assert "Львівська" in alerts[0]
    assert "«Відібрано»" in alerts[0]
    assert fake.moves[-1] == (20001, 2)


async def test_a_recruiters_decision_is_not_moved_by_a_chat_verdict(db, crm, alerts):
    await _seed(db)
    fake = crm(alive=True, live_stage=10)

    await handle_tg_outcome(peer_id="915078090", name="", username=None,
                            phone="+380671234567", verdict="not_fit", region="Київ",
                            age=30, summary="- Київ", transcript=DIALOG, reason="not_target")

    assert fake.moves == []


async def test_not_fit_does_not_ping_the_recruiters(db, crm, alerts):
    await _seed(db)
    crm(alive=True)

    await handle_tg_outcome(peer_id="915078090", name="", username=None,
                            phone="+380671234567", verdict="not_fit", region="Київ",
                            age=30, summary="- Київ", transcript=DIALOG,
                            reason="not_target")

    assert alerts == []


def test_verdict_with_bullet_list_summary_is_accepted():
    # The classifier is asked for "1-2 bullet points" and sometimes returns them as
    # a JSON list. The API answered 422 and the verdict never reached the card:
    # on 28.09 three rejected candidates stayed in «Відібрано» that way.
    from src.api.schemas import TgOutcomePayload

    p = TgOutcomePayload(peer_id="1", verdict="not_fit", summary=["Досвід менше року", "Львів"],
                         age="29 років", region=["Львівська"])

    assert p.summary == "- Досвід менше року\n- Львів"
    assert p.age == 29
    assert p.region == "Львівська"


async def test_qualified_while_hiring_is_paused_goes_to_the_reserve(db, crm, alerts, monkeypatch):
    await _seed(db)
    fake = crm(alive=True)
    monkeypatch.setattr(tg_outcome, "hiring_paused", lambda: True)

    await handle_tg_outcome(peer_id="915078090", name="", username=None,
                            phone="+380671234567", verdict="qualified", region="Львівська",
                            age=29, summary="- продажі 2 роки", transcript=DIALOG)

    assert fake.moves[-1] == (10559, 82)
    assert "резерв" in alerts[0]
