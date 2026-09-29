"""/pause in the admin bot is the one switch that stops Eva reaching out.

The client pauses from Telegram. Until 28.09 /pause stopped only the dialer:
cold sourcing kept spending paid work.ua opens, the Telegram outreach kept
writing, and robota.ua kept opening paid contacts. Applications from the job
boards keep arriving either way.
"""
from __future__ import annotations

import pytest

from src.integrations import robotaua_sync


@pytest.fixture
def paused(monkeypatch):
    monkeypatch.setattr("src.bot.admin.calls_paused", lambda: True)


async def test_cold_sourcing_spends_nothing_while_paused(paused, monkeypatch):
    from src.scraper import cold_sourcing

    monkeypatch.setattr(cold_sourcing.get_settings(), "workua_cold_sourcing_enabled", True)

    assert await cold_sourcing.run_cold_sourcing_cycle() == {"skipped": "paused"}


async def test_telegram_outreach_waits_while_paused(paused, monkeypatch):
    from src.integrations import tg_outreach
    from src.scheduler import dispatcher

    ran = []

    async def fake_run_once(**kw):
        ran.append(kw)
    monkeypatch.setattr(tg_outreach, "run_once", fake_run_once)
    monkeypatch.setenv("OUTREACH_BACKFILL_AFTER", "2026-08-18T13:55")

    await dispatcher.send_backfill_outreach()

    assert ran == []


def test_no_paid_robotaua_open_while_paused(paused):
    apply = {"vacancyId": 11277559, "resumeType": "AttachedFile"}
    assert robotaua_sync.worth_opening(apply, None) is False


def test_paid_robotaua_open_resumes_with_the_switch(monkeypatch):
    monkeypatch.setattr("src.bot.admin.calls_paused", lambda: False)
    apply = {"vacancyId": 11277559, "resumeType": "AttachedFile"}
    route = robotaua_sync.vacancies.for_robotaua(11277559)
    assert robotaua_sync.worth_opening(apply, None) is bool(route is None or route.open_paid_contacts)


# 29.09 the client: "stop Eva until we say so". /pause did not reach the three
# places Eva still spoke on her own: inbound calls (a full screening), Telegram
# replies, and the robota.ua chat.

async def test_an_inbound_call_hears_one_line_and_no_screening(paused):
    from src.api.schemas import VapiWebhookPayload
    from src.api.services import handle_assistant_request

    res = await handle_assistant_request(
        VapiWebhookPayload(type="assistant-request", customer_phone="+380671234567"))

    over = res["assistantOverrides"]
    assert "призупинено" in over["firstMessage"]
    assert over["maxDurationSeconds"] <= 30


async def test_telegram_eva_is_quiet_and_the_recruiters_get_the_message(paused):
    from src.api.tg_gate import decide

    d = await decide(None, crm=None)

    assert (d.engage, d.notify, d.why) == (False, True, "paused")


def test_the_forwarded_message_says_eva_is_stopped():
    from src.api.tg_silenced import alert_text

    text = alert_text(name="Анна", username=None, phone=None, lead_id=None, stage=None,
                      messages=["Добрий день"], paused=True)

    assert "на паузі" in text
    assert "Картка вже у вас" not in text


async def test_no_reminders_or_closings_while_paused(paused):
    from src.api.tg_silence import handle_tg_silence

    res = await handle_tg_silence(peer_id="1", phone=None, action="check", crm=object())

    assert res["eligible"] is False and res["why"] == "paused"


async def test_robotaua_chat_asks_nobody_while_paused(paused, monkeypatch):
    from src.integrations import robotaua_chat

    monkeypatch.setenv("ROBOTAUA_CHAT_REPLY_ENABLED", "1")

    assert await robotaua_chat._maybe_reply(None, "c1", [], {}, {}, None, dry=False) is False
