"""Cold sourcing over the work.ua API.

The point of this path is that the expensive step is the *last* one: search and
filter are free, and a paid contact open happens only for someone who already
passed the portrait. These tests pin that order, because getting it backwards
burns the day's credits on people we would have discarded anyway — which is
what the browser path did every time it opened a contact before checking the
region.
"""
from __future__ import annotations

import pytest

from src.integrations import workua_resume_search as api_src


def _rec(rid: str, *, region: str, first="Іван", last="Петренко", born="1990-05-05", name="Менеджер з продажу"):
    return {
        "resume_id": rid,
        "first_name": first,
        "last_name": last,
        "birth_date": born,
        "region": region,
        "name": name,
        "positions": [name, "Торговий представник"],
    }


class _FakeClient:
    """Stands in for WorkUaClient, counting the calls that cost money."""

    def __init__(self, pages, resume_payloads=None, blocked=None):
        self._pages = pages
        self._blocked = blocked or {}
        self._resumes = resume_payloads or {}
        self.searches = []
        self.opens = []

    async def search_resumes(self, **kw):
        self.searches.append(kw)
        page = kw.get("page", 1)
        items = self._pages.get(page, [])
        blocked = self._blocked.get(page, [])
        return {"result": items, "errors": blocked, "allCount": 999}

    async def get_dictionary(self, name):
        # Shape mirrors work.ua's real /dictionaries payload: Russian town names
        # with numeric ids, nested under a wrapper key.
        return {
            "result": [
                {"id": 34, "name": "Днепр"},
                {"id": 44, "name": "Львов"},
                {"id": 43, "name": "Луцк"},
                {"id": 4, "name": "Киев"},
            ]
        }

    async def get_resume(self, resume_id):
        self.opens.append(resume_id)
        return self._resumes.get(int(resume_id), {"result": {"phone": "0501461979"}})

    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(*a, **kw):
        return None

    monkeypatch.setattr(api_src.asyncio, "sleep", _instant)


@pytest.fixture
def _regions(monkeypatch):
    class _S:
        regions_allowed = {"Дніпропетровська", "Львівська"}
        regions_blocked = {"Київська", "м. Київ"}
        workua_cold_sourcing_by_region = True
        workua_cold_sourcing_pages_per_query = 3
        workua_cold_sourcing_max_pages_per_run = 150

    monkeypatch.setattr(api_src, "get_settings", lambda: _S())
    return _S


@pytest.mark.asyncio
async def test_search_is_free_of_charge_it_never_asks_for_phones(_regions):
    client = _FakeClient({1: [_rec("1", region="Дніпро")]})
    await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=5, open_budget=5, client=client
    )
    assert client.searches, "no search was performed"
    assert all(call["with_phone"] is False for call in client.searches)


@pytest.mark.asyncio
async def test_rejected_regions_never_reach_the_paid_step(_regions):
    client = _FakeClient({1: [_rec("1", region="Київ"), _rec("2", region="Дніпро")]})
    out = await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=10, open_budget=10, client=client
    )
    assert client.opens == [2], "only the allowed region should have cost a credit"
    assert len(out) == 1
    assert out[0].region == "Дніпро"


@pytest.mark.asyncio
async def test_open_budget_is_a_hard_cap(_regions):
    page = [_rec(str(i), region="Дніпро") for i in range(1, 11)]
    client = _FakeClient({1: page})
    out = await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=10, open_budget=3, client=client
    )
    assert len(client.opens) == 3
    assert len(out) == 3


@pytest.mark.asyncio
async def test_a_candidate_without_a_phone_is_dropped_not_returned(_regions):
    client = _FakeClient(
        {1: [_rec("7", region="Дніпро")]},
        resume_payloads={7: {"result": {"about": "нічого корисного"}}},
    )
    out = await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=5, open_budget=5, client=client
    )
    assert client.opens == [7]
    assert out == []


@pytest.mark.asyncio
async def test_phone_is_normalised_to_e164(_regions):
    client = _FakeClient(
        {1: [_rec("9", region="Львів")]},
        resume_payloads={9: {"result": {"phone": "050 146-19-79"}}},
    )
    out = await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=5, open_budget=5, client=client
    )
    assert out[0].phone_e164 == "+380501461979"


@pytest.mark.asyncio
async def test_the_same_resume_is_never_opened_twice_across_queries(_regions):
    client = _FakeClient({1: [_rec("5", region="Дніпро")]})
    out = await api_src.search_resumes_via_api(
        queries=["менеджер", "продаж", "логіст"], total_limit=10, open_budget=10, client=client
    )
    assert client.opens == [5]
    assert len(out) == 1


@pytest.mark.asyncio
async def test_listing_carries_what_the_recruiter_needs(_regions):
    client = _FakeClient({1: [_rec("11", region="Дніпро", first="Олена", last="Ткач")]})
    out = await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=5, open_budget=5, client=client
    )
    listing = out[0]
    assert listing.full_name == "Олена Ткач"
    assert listing.work_ua_url == "https://www.work.ua/resumes/11/"
    assert "Менеджер з продажу" in (listing.desired_position or "")


def test_age_is_derived_from_the_birth_date():
    from datetime import date

    assert api_src._age_from_birth_date("1990-01-01") == date.today().year - 1990 - (
        (date.today().month, date.today().day) < (1, 1)
    )
    assert api_src._age_from_birth_date(None) is None
    assert api_src._age_from_birth_date("not-a-date") is None


def test_phone_extraction_survives_the_key_moving():
    assert api_src._extract_phone({"result": {"phone": "0501461979"}})
    assert api_src._extract_phone({"result": {"contacts": {"mobile": "+380501461979"}}})
    assert api_src._extract_phone({"result": {"nothing": "here"}}) is None


@pytest.mark.asyncio
async def test_prescreen_runs_before_any_credit_is_spent(_regions):
    """The whole economics of this path: a candidate the filters would reject
    must be rejected while looking at them is still free. The first live run
    opened six contacts and discarded all six afterwards."""
    client = _FakeClient({1: [_rec("1", region="Дніпро", name="Кухар"),
                              _rec("2", region="Дніпро", name="Менеджер з продажу")]})
    seen = []

    async def only_sales(listing):
        seen.append(listing.desired_position)
        return "продаж" in (listing.desired_position or "").lower()

    out = await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=10, open_budget=10,
        prescreen=only_sales, client=client,
    )
    assert len(seen) == 2, "prescreen must see every accessible candidate"
    assert client.opens == [2], "only the accepted candidate may cost a credit"
    assert len(out) == 1


@pytest.mark.asyncio
async def test_prescreen_sees_a_listing_without_a_phone(_regions):
    """It runs before the paid step, so the phone cannot be there yet — a
    prescreen that assumed otherwise would silently reject everyone."""
    client = _FakeClient({1: [_rec("3", region="Дніпро")]})
    captured = {}

    async def check(listing):
        captured["phone"] = listing.phone_e164
        captured["name"] = listing.full_name
        return True

    await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=5, open_budget=5, prescreen=check, client=client
    )
    assert captured["phone"] is None
    assert captured["name"] == "Іван Петренко"


@pytest.mark.asyncio
async def test_a_failing_prescreen_rejects_rather_than_spends(_regions):
    client = _FakeClient({1: [_rec("4", region="Дніпро")]})

    async def broken(listing):
        raise RuntimeError("scorer is down")

    out = await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=5, open_budget=5, prescreen=broken, client=client
    )
    assert client.opens == [], "a broken screen must never fall through to paying"
    assert out == []


@pytest.mark.asyncio
async def test_it_asks_work_ua_for_our_cities_not_the_whole_country(_regions):
    """Searching nationwide wasted every request.

    On 18.09 a full-country sweep returned 7 openable people and all 7 lived
    somewhere we do not hire, so the run ingested nobody. Asking per town id
    means the filter afterwards has nothing left to throw away.
    """
    client = _FakeClient({1: [_rec("1", region="Дніпро")]})
    await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=50, open_budget=0, client=client
    )
    used = {call.get("region_id") for call in client.searches}
    assert None not in used, "a nationwide search slipped through"
    # Дніпро and Львів are the whitelisted oblasts in the fixture; Київ is not.
    assert used == {34, 44}, f"searched the wrong towns: {used}"


@pytest.mark.asyncio
async def test_a_page_of_locked_resumes_does_not_end_the_search(_regions):
    """Roughly 97% of resumes cannot be opened without a CV-database
    subscription, so pages where every row is locked are the normal case. The
    old loop treated an empty `result` as "end of results" and stopped at the
    first such page — which is why deeper, richer pages were never reached."""
    client = _FakeClient(
        pages={1: [], 2: [], 3: [_rec("7", region="Дніпро")]},
        blocked={1: [{"region": "Дніпро"}] * 20, 2: [{"region": "Дніпро"}] * 20},
    )
    out = await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=50, open_budget=5, client=client
    )
    assert [o.full_name for o in out] == ["Іван Петренко"], "page 3 was never reached"


@pytest.mark.asyncio
async def test_a_genuinely_empty_page_still_stops_the_sweep(_regions):
    client = _FakeClient(pages={1: [_rec("1", region="Дніпро")]})
    await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=50, open_budget=0, client=client
    )
    pages_per_city = [c["page"] for c in client.searches if c.get("region_id") == 34]
    assert pages_per_city == [1, 2], "kept paging past the end of the results"


@pytest.mark.asyncio
async def test_a_fruitless_run_still_ends(_regions, monkeypatch):
    """Twelve cities times six pages times several queries is a long walk at
    1.5s a request. The budget is what guarantees the 06:45 job finishes."""
    class _S:
        regions_allowed = {"Дніпропетровська", "Львівська"}
        regions_blocked = set()
        workua_cold_sourcing_by_region = True
        workua_cold_sourcing_pages_per_query = 50
        workua_cold_sourcing_max_pages_per_run = 7

    monkeypatch.setattr(api_src, "get_settings", lambda: _S())
    client = _FakeClient(
        pages={},
        blocked={n: [{"region": "Дніпро"}] * 20 for n in range(1, 60)},
    )
    out = await api_src.search_resumes_via_api(
        queries=["менеджер", "логіст"], total_limit=50, open_budget=5, client=client
    )
    assert out == []
    assert len(client.searches) == 7, f"budget ignored: {len(client.searches)} requests"


@pytest.mark.asyncio
async def test_it_falls_back_to_a_nationwide_search_if_the_dictionary_fails(_regions):
    """A work.ua outage on the dictionary endpoint must degrade to the old
    behaviour, not skip the run."""
    client = _FakeClient({1: [_rec("1", region="Дніпро")]})

    async def _boom(name):
        raise RuntimeError("503")

    client.get_dictionary = _boom
    out = await api_src.search_resumes_via_api(
        queries=["менеджер"], total_limit=5, open_budget=5, client=client
    )
    assert all(c.get("region_id") is None for c in client.searches)
    assert len(out) == 1
