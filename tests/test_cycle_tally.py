"""The daily report must describe the whole cycle, not its last vacancy.

This morning's run looked at 27 resumes and paid to open 10 contacts across two
vacancies. The report said «переглянуто 4 … відкрито 1» — the second vacancy's
numbers, written last over the first vacancy's. A report that undercounts its
own work by a factor of seven is worse than no report: it was the number the
recruiter would have used to decide whether cold sourcing was worth keeping.
"""
from __future__ import annotations

import pytest

from src.integrations import workua_resume_search as api_src


class _Client:
    def __init__(self, items):
        self._items = items

    async def search_resumes(self, **kw):
        page = kw.get("page", 1)
        return {"result": self._items if page == 1 else [], "errors": [], "allCount": 1}

    async def get_dictionary(self, name):
        return {"result": [{"id": 34, "name": "Днепр"}]}

    async def get_resume(self, resume_id):
        return {"result": {"phone": "0501461979"}}

    async def aclose(self):
        pass


def _rec(rid):
    return {
        "resume_id": rid, "first_name": "Іван", "last_name": "Петренко",
        "birth_date": "1990-01-01", "region": "Дніпро",
        "name": "Менеджер з продажу", "positions": ["Менеджер з продажу"],
    }


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    async def _instant(*a, **kw):
        return None

    monkeypatch.setattr(api_src.asyncio, "sleep", _instant)

    class _S:
        regions_allowed = {"Дніпропетровська"}
        regions_blocked = set()
        workua_cold_sourcing_by_region = True
        workua_cold_sourcing_pages_per_query = 2
        workua_cold_sourcing_max_pages_per_run = 50

    monkeypatch.setattr(api_src, "get_settings", lambda: _S())


@pytest.mark.asyncio
async def test_counts_accumulate_across_calls_instead_of_overwriting():
    tally: dict = {}
    await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=9, open_budget=9,
        client=_Client([_rec("1"), _rec("2"), _rec("3")]), tally=tally,
    )
    await api_src.search_resumes_via_api(
        queries=["логіст"], total_limit=9, open_budget=9,
        client=_Client([_rec("4")]), tally=tally,
    )
    assert tally["urls_found"] == 4, f"second call overwrote the first: {tally}"
    assert tally["contact_opens_spent"] == 4


@pytest.mark.asyncio
async def test_a_tallied_call_writes_no_breadcrumb_of_its_own(monkeypatch):
    """Whoever owns the tally owns the write; otherwise we are back to the last
    vacancy having the final word."""
    written = []
    monkeypatch.setattr(api_src, "record_cold_sourcing_counts", lambda c: written.append(c))
    await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=5, open_budget=5,
        client=_Client([_rec("1")]), tally={},
    )
    assert written == []


@pytest.mark.asyncio
async def test_a_standalone_call_still_records_itself(monkeypatch):
    written = []
    monkeypatch.setattr(api_src, "record_cold_sourcing_counts", lambda c: written.append(c))
    await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=5, open_budget=5,
        client=_Client([_rec("1")]),
    )
    assert len(written) == 1
    assert written[0]["urls_found"] == 1
