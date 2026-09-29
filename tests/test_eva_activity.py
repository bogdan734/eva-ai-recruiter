"""What Eva did with a candidate, readable on the lead card itself.

29.09 the client: how many times Eva called and whether she wrote is visible only
on a saved buyer's card. On the lead card it went into the note, and KeyCRM keeps
a card's note as it was at creation -- every later edit is accepted and dropped.
Custom fields do update, so the line now opens «AI Summary».
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.common import activity
from src.common.activity import activity_line, tg_failed_note, tg_sent_note, with_activity
from src.common.keycrm import FIELD_AI_SUMMARY, KeyCRMClient
from src.common.models import Base, Call, CallStatus, Candidate


def test_the_line_says_calls_talk_and_telegram():
    assert activity_line(calls=2, talked_sec=45, tg_note="написала 29.09") == (
        "🤖 Єва: дзвінків 2 · найдовша розмова 0:45 · Telegram: написала 29.09"
    )


def test_nothing_done_is_said_plainly():
    assert activity_line(calls=0, talked_sec=0, tg_note=None) == (
        "🤖 Єва: не телефонувала · Telegram: не писала"
    )


def test_the_line_opens_the_summary_and_replaces_an_older_one():
    first = with_activity("• Досвід 2 роки", "🤖 Єва: дзвінків 1 · Telegram: не писала")
    assert first == "🤖 Єва: дзвінків 1 · Telegram: не писала\n\n• Досвід 2 роки"

    second = with_activity(first, "🤖 Єва: дзвінків 2 · Telegram: написала 29.09")
    assert second == "🤖 Єва: дзвінків 2 · Telegram: написала 29.09\n\n• Досвід 2 роки"
    assert with_activity(None, "🤖 Єва: не телефонувала") == "🤖 Єва: не телефонувала"


def test_telegram_outcomes_in_plain_words():
    assert tg_sent_note(datetime(2026, 9, 29, 10, 0)) == "написала 29.09"
    assert tg_failed_note("номер не в Telegram або приховує телефон") == (
        "не вдалося написати — немає в Telegram")
    assert tg_failed_note("приватність: не приймає повідомлення") == (
        "не вдалося написати — закрив повідомлення від незнайомих")
    assert tg_failed_note("privacy_premium_required") == (
        "не вдалося написати — закрив повідомлення від незнайомих")
    # a daily limit or a flood wait says nothing about the person
    assert tg_failed_note("FloodWait 30s") is None
    assert tg_failed_note("Вже писали цьому кандидату (анти-спам)") is None


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

    monkeypatch.setattr(activity, "session_scope", scope)
    yield maker
    await engine.dispose()


async def test_the_line_is_counted_from_the_calls_table(db):
    async with db() as session:
        cand = Candidate(full_name="Долгош Наталія", phone_e164="+380500000011",
                         tg_note="не вдалося написати — немає в Telegram")
        session.add(cand)
        await session.flush()
        for sec in (0, 46, 213):
            session.add(Call(candidate_id=cand.id, attempt_number=1,
                             status=CallStatus.HANGUP, duration_sec=sec))
        await session.commit()
        cid = cand.id

    assert await activity.line_for(cid) == (
        "🤖 Єва: дзвінків 3 · найдовша розмова 3:33 · "
        "Telegram: не вдалося написати — немає в Telegram"
    )


async def test_the_card_keeps_its_summary_under_the_new_line():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": 7, "custom_fields": [
                {"uuid": FIELD_AI_SUMMARY, "value": "🤖 Єва: дзвінків 1 · Telegram: не писала\n\n• Досвід"}]})
        seen.append(json.loads(request.content))
        return httpx.Response(202, json={"status": True})

    kc = KeyCRMClient(token="t", base_url="https://keycrm.test")
    kc._client = httpx.AsyncClient(base_url="https://keycrm.test",
                                   transport=httpx.MockTransport(handler))
    await kc.set_activity_line(7, "🤖 Єва: дзвінків 2 · Telegram: написала 29.09")
    await kc.aclose()

    assert seen == [{"custom_fields": [{
        "uuid": FIELD_AI_SUMMARY,
        "value": "🤖 Єва: дзвінків 2 · Telegram: написала 29.09\n\n• Досвід",
    }]}]
