"""src/scraper/cold_sourcing.py — the orchestration that ties vacancy registry
+ resume search + the intake pipeline together, run daily by the scheduler.

Uses fake, isolated Vacancy objects (not the live registry) so these tests
stay correct regardless of future registry edits, and mocks search_resumes /
feed_resumes_to_pipeline so no Playwright browser or real DB is needed.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.common.vacancies import Vacancy
from src.scraper import cold_sourcing


def _vacancy(key: str, *, calls_enabled=True, intake_enabled=True, role_markers=("продаж",)) -> Vacancy:
    return Vacancy(
        key=key, label=f"Test {key}", workua_ids=frozenset(), robotaua_ids=frozenset(),
        keycrm_pipeline_id=1, keycrm_status_id=1,
        calls_enabled=calls_enabled, screen_enabled=True,
        intake_enabled=intake_enabled, role_markers=role_markers,
    )


@dataclass
class _FakeSettings:
    workua_cold_sourcing_enabled: bool = True
    workua_cold_sourcing_max_per_run: int = 15
    workua_scrape_daily_limit: int = 50
    # These tests mock the browser source, so they exercise that branch. The
    # API branch has its own file (test_workua_api_sourcing.py); what matters
    # here is the orchestration around the source, which is shared.
    workua_cold_sourcing_use_api: bool = False
    workua_max_contact_opens_per_run: int = 10
    cold_sourcing_match_threshold: float = 0.45


@pytest.mark.asyncio
async def test_disabled_flag_skips_everything(monkeypatch):
    monkeypatch.setattr(cold_sourcing, "get_settings", lambda: _FakeSettings(workua_cold_sourcing_enabled=False))
    called = False

    async def _boom(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(cold_sourcing, "search_resumes", _boom)
    result = await cold_sourcing.run_cold_sourcing_cycle()
    assert result == {"skipped": "disabled"}
    assert called is False


@pytest.mark.asyncio
async def test_non_calling_and_intake_disabled_vacancies_are_skipped(monkeypatch):
    monkeypatch.setattr(cold_sourcing, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(cold_sourcing, "all_vacancies", lambda: {
        "manual_only": _vacancy("manual_only", calls_enabled=False),
        "owned_elsewhere": _vacancy("owned_elsewhere", intake_enabled=False),
    })
    searched = []

    async def fake_search(**kwargs):
        searched.append(kwargs)
        return []

    monkeypatch.setattr(cold_sourcing, "search_resumes", fake_search)
    result = await cold_sourcing.run_cold_sourcing_cycle()
    assert searched == []
    assert result["by_vacancy"] == {}


@pytest.mark.asyncio
async def test_vacancy_without_role_markers_is_skipped(monkeypatch):
    monkeypatch.setattr(cold_sourcing, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(cold_sourcing, "all_vacancies", lambda: {
        "no_markers": _vacancy("no_markers", role_markers=()),
    })
    searched = []
    monkeypatch.setattr(cold_sourcing, "search_resumes", lambda **kw: searched.append(kw))
    result = await cold_sourcing.run_cold_sourcing_cycle()
    assert searched == []
    assert result["by_vacancy"]["no_markers"] == {"skipped": "no_role_markers"}


@pytest.mark.asyncio
async def test_shared_budget_stops_the_next_vacancy_once_spent(monkeypatch):
    """The per-run cap is shared across vacancies, spent in registry order —
    a big first vacancy must not let a later one overflow the queue."""
    monkeypatch.setattr(cold_sourcing, "get_settings", lambda: _FakeSettings(workua_cold_sourcing_max_per_run=5))
    monkeypatch.setattr(cold_sourcing, "all_vacancies", lambda: {
        "sales": _vacancy("sales"),
        "logistics_jr": _vacancy("logistics_jr"),
    })

    async def fake_search(**kwargs):
        return ["listing"] * 10  # plenty available, more than the budget

    fed_calls = []

    async def fake_feed(listings, **kwargs):
        fed_calls.append((len(listings), kwargs.get("vacancy_key")))
        return {"received": len(listings), "ingested": len(listings), "matched": len(listings),
                "duplicates": 0, "rejected": 0, "profile_rejected": 0}

    monkeypatch.setattr(cold_sourcing, "search_resumes", fake_search)
    monkeypatch.setattr(cold_sourcing, "feed_resumes_to_pipeline", fake_feed)

    result = await cold_sourcing.run_cold_sourcing_cycle()

    # sales spends the whole budget (5 of the 10 available), logistics_jr never
    # gets a turn because remaining hit 0 before its iteration.
    assert fed_calls == [(5, "sales")]
    assert result["totals"]["ingested"] == 5
    assert "logistics_jr" not in result["by_vacancy"]


@pytest.mark.asyncio
async def test_one_vacancys_search_failure_does_not_block_the_next(monkeypatch):
    monkeypatch.setattr(cold_sourcing, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(cold_sourcing, "all_vacancies", lambda: {
        "broken": _vacancy("broken"),
        "fine": _vacancy("fine"),
    })

    async def fake_search(**kwargs):
        raise RuntimeError("playwright blew up")

    calls = []
    monkeypatch.setattr(cold_sourcing, "search_resumes", fake_search)
    monkeypatch.setattr(cold_sourcing, "feed_resumes_to_pipeline", lambda *a, **k: calls.append(1))

    result = await cold_sourcing.run_cold_sourcing_cycle()
    assert "error" in result["by_vacancy"]["broken"]
    assert "error" in result["by_vacancy"]["fine"]
    assert calls == []
