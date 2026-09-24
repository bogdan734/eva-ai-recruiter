"""Cold-sourcing building blocks in src/scraper/workua.py.

No password is ever entered by this code — a human logs into work.ua once and
hands it the resulting session (Playwright storage_state, or a plain cookie
export from a browser extension). These tests cover: normalizing whichever
shape the session file is in, never raising on a missing/bad file (this runs
on an unattended daily schedule), and pulling resume links out of a search
page by URL shape rather than a brittle CSS selector.
"""
from __future__ import annotations

import json

import pytest

from src.scraper import workua


# --- session loading ---------------------------------------------------------

def test_storage_state_passthrough_gets_origins_key():
    state = {"cookies": [{"name": "a", "value": "b"}]}
    out = workua._cookie_list_to_storage_state(state)
    assert out["cookies"] == state["cookies"]
    assert out["origins"] == []


def test_cookie_array_export_is_normalized():
    raw = [
        {
            "name": "session_id", "value": "abc123", "domain": ".work.ua",
            "path": "/", "expirationDate": 1999999999.0,
            "httpOnly": True, "secure": True, "sameSite": "no_restriction",
        },
        {"name": "", "value": "should be dropped"},  # missing name -> skipped
    ]
    out = workua._cookie_list_to_storage_state(raw)
    assert len(out["cookies"]) == 1
    c = out["cookies"][0]
    assert c["name"] == "session_id"
    assert c["value"] == "abc123"
    assert c["sameSite"] == "None"  # normalized from "no_restriction"
    assert c["httpOnly"] is True


def test_cookie_array_defaults_domain_and_path():
    out = workua._cookie_list_to_storage_state([{"name": "x", "value": "y"}])
    c = out["cookies"][0]
    assert c["domain"] == ".work.ua"
    assert c["path"] == "/"
    assert c["sameSite"] == "Lax"


def test_unrecognized_shape_raises():
    with pytest.raises(ValueError):
        workua._cookie_list_to_storage_state("not a session at all")


def test_load_session_state_missing_file_returns_none(tmp_path):
    assert workua.load_session_state(tmp_path / "does_not_exist.json") is None


def test_load_session_state_malformed_json_returns_none_not_raise(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert workua.load_session_state(p) is None


def test_load_session_state_empty_cookies_returns_none(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    assert workua.load_session_state(p) is None


def test_load_session_state_valid_file_roundtrips(tmp_path):
    p = tmp_path / "good.json"
    p.write_text(
        json.dumps([{"name": "s", "value": "v", "domain": ".work.ua"}]),
        encoding="utf-8",
    )
    state = workua.load_session_state(p)
    assert state is not None
    assert state["cookies"][0]["name"] == "s"


def test_session_available_reflects_file_presence(tmp_path):
    missing = tmp_path / "none.json"
    assert workua.session_available(missing) is False
    present = tmp_path / "session.json"
    present.write_text(json.dumps([{"name": "s", "value": "v"}]), encoding="utf-8")
    assert workua.session_available(present) is True


# --- search result parsing ---------------------------------------------------

def test_extract_resume_urls_dedupes_and_matches_by_shape():
    html = '''
    <a href="/resumes/1234567/">Іван Іванов</a>
    <a href="/resumes/1234567/">повторне посилання</a>
    <a href="/resumes/7654321/print/">версія для друку</a>
    <a href="/jobs/8249916/">не резюме — вакансія, ігнорується</a>
    <a href="/resumes-search/">сторінка пошуку, не резюме</a>
    '''
    urls = workua._extract_resume_urls(html)
    assert urls == [
        "https://www.work.ua/resumes/1234567/",
        "https://www.work.ua/resumes/7654321/print/",
    ]


def test_extract_resume_urls_empty_page():
    assert workua._extract_resume_urls("<html><body>нічого</body></html>") == []


def test_search_url_has_no_page_param_on_first_page():
    from urllib.parse import unquote

    url = workua._search_url("менеджер з продажу", page=1)
    assert not url.endswith("?page=1")
    slug = url.removeprefix("https://www.work.ua/resumes-").removesuffix("/")
    # Spaces become literal "+" (work.ua's slug convention), not %20 or %2B —
    # unquoting must hand back the original words with "+" as the separator.
    assert unquote(slug) == "менеджер+з+продажу"


def test_search_url_paginates():
    url = workua._search_url("логіст", page=2)
    assert url.endswith("?page=2")


# --- graceful no-op without a session ---------------------------------------

@pytest.mark.asyncio
async def test_search_resumes_returns_empty_without_session(tmp_path):
    """Regression: cold sourcing must never crash the scheduler when nobody
    has generated a session file yet — it should just skip, silently."""
    result = await workua.search_resumes(
        queries=["продаж"], session_state_path=tmp_path / "missing.json",
    )
    assert result == []
