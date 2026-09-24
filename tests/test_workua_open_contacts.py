"""Cold-sourcing phone extraction and contact-opening budget
(src/scraper/workua.py).

07.09.2026, discovered live with the recruiter's own logged-in Chrome
session: a candidate's phone is invisible on work.ua until an employer
explicitly clicks "Відкрити контакти" -- the original `_parse_resume_page`
only ever regex-scanned the page for an already-visible number, so cold
sourcing would find candidates but ingest zero phones, silently rejected
downstream. Also found the same live session: work.ua renders phones in
LOCAL format ("098 495-59-85"), not "+380..." -- the old phone regex
(`\\+?380[\\d\\s\\-()]{9,}`) could never have matched a real resume page even
after opening contacts.

These tests cover the fix's two halves separately: the (now label-anchored,
local-format-aware) phone regex, and the click/budget logic -- with a fake
Page so no real browser or network is needed.
"""
from __future__ import annotations

import pytest

from src.scraper import workua


# --- phone regex --------------------------------------------------------

def test_matches_local_format_after_telefon_label():
    body = "<dt>Телефон:</dt><dd>098 495-59-85</dd>"
    m = workua._PHONE_RE.search(body)
    assert m is not None
    assert m.group(1).strip() == "098 495-59-85"


def test_matches_international_format_too():
    body = "Телефон: +380 98 495 59 85"
    m = workua._PHONE_RE.search(body)
    assert m is not None


def test_does_not_match_the_still_hidden_placeholder():
    body = "Телефон: [відкрити контакти]​"
    assert workua._PHONE_RE.search(body) is None


def test_prefers_the_first_telefon_occurrence_over_a_later_ocr_quirk():
    """Real live shape: the structured contact field renders before the
    uploaded-file quick-view's OCR'd text, which can carry a typo'd digit."""
    body = (
        "Контактна інформація ... Телефон:\n098 495-59-85 ... "
        "Костюченко Ігор Миколайович Телефон: 098-495-59-05"
    )
    m = workua._PHONE_RE.search(body)
    assert m is not None
    assert "85" in m.group(1)  # the real field, not the OCR'd "...05" later on


def test_matches_real_workua_markup_gap():
    """The exact shape a really-opened contact renders as, captured live on
    09.09.2026 from resume 19824550. The number sits 274 characters past the
    label; the original 120-char window could not reach it, so cold sourcing
    opened contacts (spending quota) and still reported no phone."""
    body = (
        "Телефон:</dt>\n                                <dd>\n"
        + " " * 60
        + '<ul class="list-unstyled my-0 flex flex-align-center flex-wrap">\n'
        + " " * 44
        + '<li class="no-style my-0 mr-xs"><span>098 171-42-69</span></li>\n'
    )
    assert body.index("098") - body.index("Телефон") > 200  # guard the guard
    m = workua._PHONE_RE.search(body)
    assert m is not None
    assert m.group(1) == "098 171-42-69"


def test_no_telefon_label_at_all_is_a_clean_no_match():
    assert workua._PHONE_RE.search("<html><body>нічого немає</body></html>") is None


# --- quota counter --------------------------------------------------------

def test_quota_counter_parses_remaining_count():
    body = "Ви можете відкрити 6 з 10 контактів, доступних на день."
    m = workua._QUOTA_RE.search(body)
    assert m is not None
    assert int(m.group(1)) == 6


def test_quota_counter_zero_remaining():
    body = "Ви можете відкрити 0 з 10 контактів, доступних на день."
    m = workua._QUOTA_RE.search(body)
    assert int(m.group(1)) == 0


# --- session heartbeat ------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_session_without_a_file_is_a_silent_noop(tmp_path, monkeypatch):
    """The heartbeat runs every 20 minutes whether or not a session exists.
    With no file it must return a plain status and never touch a browser --
    otherwise every recruiter-less day would spawn 72 pointless Chromiums."""
    from src.scraper import workua as w

    class _S:
        workua_session_state_path = str(tmp_path / "nope.json")
        workua_proxy_url = ""

    monkeypatch.setattr(w, "get_settings", lambda: _S())

    def _boom(*a, **kw):
        raise AssertionError("must not launch a browser without a session")

    monkeypatch.setattr(w, "async_playwright", _boom)
    assert await w.refresh_session() == "no_session"


def test_challenge_detector_recognises_every_cloudflare_variant():
    """10.09.2026: the first unattended run reported "ok, found nothing" against
    a page that was actually a Cloudflare block. Only the Ukrainian wording was
    covered; the English-only body shares none of those strings, and the Ray ID
    is the one thing present on all of them."""
    for body in (
        "<title>Трохи зачекайте…</title>",
        "Enable JavaScript and cookies to continue",
        "<h1>Just a moment...</h1>",
        "Attention Required! | Cloudflare",
        "Перевірка надійності підключення до сайту",
        "<div>Cloudflare Ray ID: a38c088b0ca872cd</div>",
    ):
        assert workua._looks_challenged(body), body[:40]
    assert not workua._looks_challenged("<html>Знайдено 120 резюме</html>")


def test_challenge_detector_recognises_the_interstitial():
    """work.ua answers a throttled client with "Трохи зачекайте…" instead of
    results; without this check that page is indistinguishable from "nobody
    matched" and cold sourcing would look healthy while returning nothing."""
    assert workua._looks_challenged("<title>Трохи зачекайте…</title>")
    assert not workua._looks_challenged("<html>Знайдено 120 резюме</html>")


# --- _maybe_open_contacts ---------------------------------------------------

_OPEN_BODY = "Контактна інформація ... Відкрити контакти ... Ви можете відкрити 6 з 10 контактів, доступних на день."
_ALREADY_OPEN_BODY = "Контактна інформація ... Телефон: 098 495-59-85"
_QUOTA_ZERO_BODY = "Відкрити контакти ... Ви можете відкрити 0 з 10 контактів, доступних на день."
_REVEALED_BODY = "Контактна інформація ... Телефон: 098 495-59-85 ... Ел. пошта: x@y.com"


class _FakePage:
    def __init__(self, content_after_click: str = _REVEALED_BODY, click_raises: bool = False):
        self.clicked_with: list[str] = []
        self._content_after_click = content_after_click
        self._click_raises = click_raises

    async def click(self, selector, timeout=None):
        self.clicked_with.append(selector)
        if self._click_raises:
            raise TimeoutError("no such element")

    async def content(self):
        return self._content_after_click


@pytest.mark.asyncio
async def test_no_budget_never_clicks():
    page = _FakePage()
    out = await workua._maybe_open_contacts(page, _OPEN_BODY, None)
    assert out == _OPEN_BODY
    assert page.clicked_with == []


@pytest.mark.asyncio
async def test_zero_remaining_budget_never_clicks():
    page = _FakePage()
    budget = workua.ContactOpenBudget(remaining=0)
    out = await workua._maybe_open_contacts(page, _OPEN_BODY, budget)
    assert out == _OPEN_BODY
    assert page.clicked_with == []
    assert budget.remaining == 0


@pytest.mark.asyncio
async def test_already_open_page_never_clicks():
    """No button text in the body -- either already opened, or this
    candidate never had a phone to reveal in the first place."""
    page = _FakePage()
    budget = workua.ContactOpenBudget(remaining=5)
    out = await workua._maybe_open_contacts(page, _ALREADY_OPEN_BODY, budget)
    assert out == _ALREADY_OPEN_BODY
    assert page.clicked_with == []
    assert budget.remaining == 5


@pytest.mark.asyncio
async def test_live_quota_zero_stops_the_whole_run_not_just_this_candidate():
    page = _FakePage()
    budget = workua.ContactOpenBudget(remaining=5)
    out = await workua._maybe_open_contacts(page, _QUOTA_ZERO_BODY, budget)
    assert out == _QUOTA_ZERO_BODY
    assert page.clicked_with == []
    assert budget.remaining == 0  # zeroed out, not just skipped this once


@pytest.mark.asyncio
async def test_successful_click_spends_one_and_returns_fresh_content(monkeypatch):
    async def _no_delay(*a, **kw):
        return None

    monkeypatch.setattr(workua, "_polite_delay", _no_delay)
    page = _FakePage(content_after_click=_REVEALED_BODY)
    budget = workua.ContactOpenBudget(remaining=5)
    out = await workua._maybe_open_contacts(page, _OPEN_BODY, budget)
    assert out == _REVEALED_BODY
    assert len(page.clicked_with) == 1
    assert budget.remaining == 4


@pytest.mark.asyncio
async def test_clicks_the_class_bound_anchor_not_the_text_wrapper(monkeypatch):
    """`text=Відкрити контакти` resolves to the wrapper div, whose centre is
    empty space beside the button -- Playwright reports a successful click, the
    budget is spent, and the contact is never opened (observed live: work.ua's
    own counter stayed at "10 з 10" after two such clicks). The real control is
    <a class="... showContacts" href="/noscript/" onclick="return false;">."""

    async def _no_delay(*a, **kw):
        return None

    monkeypatch.setattr(workua, "_polite_delay", _no_delay)
    page = _FakePage(content_after_click=_REVEALED_BODY)
    budget = workua.ContactOpenBudget(remaining=5)
    await workua._maybe_open_contacts(page, _OPEN_BODY, budget)
    assert page.clicked_with == ["a.showContacts"]


@pytest.mark.asyncio
async def test_click_failure_spends_nothing_and_keeps_old_body():
    page = _FakePage(click_raises=True)
    budget = workua.ContactOpenBudget(remaining=5)
    out = await workua._maybe_open_contacts(page, _OPEN_BODY, budget)
    assert out == _OPEN_BODY
    assert budget.remaining == 5
