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
