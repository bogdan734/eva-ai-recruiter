"""work.ua scraper — public resume pages, plus authenticated search/cold-sourcing.

`scrape_resume_urls()` / `_parse_resume_page()` work against PUBLIC resume pages,
where work.ua hides candidate phone numbers from anyone not logged in as an
employer -- fine for re-fetching a URL you already have, useless for cold
sourcing on its own.

`search_resumes()` adds the other half: searching the resume database and
reading phone numbers, both of which need an authenticated employer session.
We deliberately never type the employer password into this automation --
see `load_session_state()`. A human logs into work.ua once, in their own
browser, and hands this code the resulting session (Playwright storage_state,
or a plain browser-extension cookie export); the scraper only ever reuses
that session, it never performs the login itself.

The Terms of Service of work.ua forbid automated access — this was a known,
accepted risk when cold sourcing was turned on (see WORKUA_COLD_SOURCING_ENABLED
and the daily/per-run limits below). Use a residential proxy
(`workua_proxy_url`) and a backup employer account if this account gets
rate-limited or banned.
"""
from __future__ import annotations

import asyncio
import json
import time
import os
import random
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.common.phone import normalize_phone
from src.common.regions import is_region_allowed, normalize_region
from src.common.settings import get_settings

log = structlog.get_logger()


@dataclass
class ResumeListing:
    work_ua_url: str
    full_name: str | None
    desired_position: str | None
    region: str | None
    experience_years: int | None
    languages: list[str]
    phone_e164: str | None
    raw_html_snippet: str | None = None


@dataclass
class ContactOpenBudget:
    """How many more "Відкрити контакти" clicks this run is allowed to spend.

    07.09.2026: discovered live (with the recruiter's own logged-in session)
    that a phone number is NEVER present on a resume page until an employer
    explicitly opens that candidate's contacts -- this spends the account's
    shared daily quota ("Ви можете відкрити 6 з 10 контактів, доступних на
    день" -- the same style of limit already handled for robota.ua). Without
    a budget, cold sourcing would either open contacts for every single
    search hit (burning the day's quota, shared with the human recruiter, in
    one run) or never open any (finding candidates with no phone number,
    silently rejected downstream). `remaining` is spent by
    `_maybe_open_contacts()`, one per resume it actually opens; the whole
    process stops opening for the rest of the run once it hits zero, and
    still finishes -- resumes past that point simply keep whatever phone (if
    any) is already visible, matching the pre-existing behaviour.
    """
    remaining: int


_EXPERIENCE_RE = re.compile(r"(\d+)\s*(?:рок|year|год)", re.IGNORECASE)
# Anchored on the "Телефон" label rather than a bare digit pattern: the page
# also contains an OCR'd "Телефон:" line inside the uploaded-file quick-view
# for file-based resumes, which can carry a typo'd digit relative to the real
# contact field (seen live: "098 495-59-85" in the real field vs
# "098-495-59-05" in the file quick-view a few hundred characters later) --
# re.search takes the FIRST match, and the real field renders first in page
# order, so anchoring like this (rather than a bare digit scan) reliably
# prefers the authoritative one. Matches both local ("0XX...") and
# international ("+380..."/"380...") shapes -- work.ua renders local shape by
# default, which the old `\+?380...`-only pattern could never match at all.
_PHONE_RE = re.compile(
    # 400, not 120: measured against a really-opened contact on 09.09.2026,
    # the number sits 274 chars past the label -- <dd><ul><li><span> plus
    # work.ua's very generous whitespace. The old window could never reach
    # it, which is why cold sourcing found candidates but never a phone.
    r"Телефон[\s\S]{0,400}?((?:\+?380|0)[\s\-]?\d[\d\s\-()]{6,14}\d)"
)
# The live quota counter work.ua shows above the button, e.g. "Ви можете
# відкрити 6 з 10 контактів, доступних на день." Checked before spending a
# click as a second, page-truth guard on top of our own per-run budget --
# catches the case where a human recruiter (or another run) already spent the
# day's quota between our budget being set and this particular resume.
_QUOTA_RE = re.compile(r"відкрити\s+(\d+)\s+з\s+\d+\s+контакт", re.IGNORECASE)
_OPEN_CONTACTS_BUTTON_TEXT = "Відкрити контакти"
# The actual clickable control. `text=` matches the wrapper div instead,
# whose centre is empty space -- see _maybe_open_contacts.
_OPEN_CONTACTS_SELECTOR = "a.showContacts"


# work.ua's bot-check interstitial. Title is "Трохи зачекайте…" and the body
# carries "Перевірка"; there are no resume links in it at all, so without this
# check a challenged page is indistinguishable from "nothing matched".
_CHALLENGE_MARKERS = (
    # Ukrainian interstitial
    "Трохи зачекайте",
    "Перевірка браузера",
    "Перевірка надійності підключення",
    # English variants -- served depending on Cloudflare's mode, and the one
    # that has none of the Ukrainian strings above
    "Just a moment",
    "Enable JavaScript and cookies to continue",
    "Attention Required",
    "Checking your browser",
    # On every Cloudflare block page in any language
    "Cloudflare Ray ID",
    "cf-error-details",
)


def _looks_challenged(body: str) -> bool:
    """Is this Cloudflare's block page rather than work.ua's own?

    Deliberately generous: mistaking a block for "nobody matched" sends us
    waiting for days while nothing works, which is exactly what nearly happened
    on 10.09.2026 -- the run reported "ok" against a page that turned out to be
    a challenge naming our IP.
    """
    return any(m in body for m in _CHALLENGE_MARKERS)


async def _polite_delay(min_s: float = 1.5, max_s: float = 4.5) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _human_delay() -> None:
    """A pause long enough to look like someone reading the page.

    Cold sourcing is once a day and unattended -- there is nothing to gain from
    hurrying, and the burst of fast requests is exactly what got this host
    challenged on 09.09.2026.
    """
    s = get_settings()
    await asyncio.sleep(
        random.uniform(
            s.workua_cold_sourcing_min_delay_sec,
            s.workua_cold_sourcing_max_delay_sec,
        )
    )


def _record_cold_sourcing_run(outcome: str, **extra) -> None:
    """Leave a breadcrumb the daily report can read.

    Without this a challenged run and a run where nobody matched look identical
    from the outside -- zero candidates either way -- which is precisely how the
    session dying went unnoticed today.
    """
    import os
    import time as _time

    # Never write real state from a test run: the suite executes inside the
    # container with production settings, so an unmocked call would leave a
    # fake breadcrumb in /state and the morning report would announce a
    # cold-sourcing outcome that never happened (seen 09.09.2026).
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    try:
        path = Path(get_settings().workua_session_state_path).parent / "workua_cold_sourcing.json"
        payload = {"outcome": outcome, "at": _time.time(), **extra}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 -- a breadcrumb must never break a run
        log.warning("scraper.workua.cold_sourcing_state_failed", error=str(e)[:120])


async def _goto_with_retry(
    page: Page, url: str, *, tries: int = 3, timeout_ms: int = 15_000
) -> None:
    """page.goto that survives a one-off network blip.

    09.09.2026: the first live cold-sourcing run returned zero candidates
    because a single `net::ERR_NETWORK_CHANGED` on the first search page made
    the caller break out of its pagination loop -- one transient blip cost the
    whole cycle, and the only trace was a single warning line. Blips hit about
    half the navigations from this host that day, so a retry is not a nicety.

    Re-raises the last error when every attempt fails, so callers keep their
    existing "log once and move on" behaviour.
    """
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except Exception as e:  # noqa: BLE001 -- retried here, re-raised below
            last = e
            log.info(
                "scraper.workua.goto_retry",
                url=url, attempt=attempt, error=str(e)[:120],
            )
            await asyncio.sleep(2.0 * attempt)
    assert last is not None
    raise last


def _proxy_config(proxy_url: str | None) -> dict[str, str] | None:
    """Turn one pasted proxy line into Playwright's proxy config.

    Residential providers hand over a single string like
    `http://user:pass@gate.example.com:7000`, but Chromium ignores credentials
    embedded in the server URL -- Playwright takes them as separate
    `username`/`password` fields. Passing the whole line through as `server`
    (what this did before) therefore connects without auth and the proxy
    refuses it, which would have looked like "the proxy does not work".

    Accepts the bare `host:port` shape too; Playwright reads that as HTTP.
    """
    from urllib.parse import urlsplit

    raw = (proxy_url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    host = parts.hostname or ""
    if not host:
        return None
    server = f"{parts.scheme}://{host}"
    if parts.port:
        server += f":{parts.port}"
    cfg: dict[str, str] = {"server": server}
    if parts.username:
        cfg["username"] = parts.username
    if parts.password:
        cfg["password"] = parts.password
    return cfg


async def _open_context(
    browser: Browser,
    proxy_url: str | None,
    storage_state: dict | None = None,
) -> BrowserContext:
    kwargs: dict[str, object] = {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1440, "height": 900},
        "locale": "uk-UA",
        "timezone_id": "Europe/Kyiv",
    }
    proxy = _proxy_config(proxy_url)
    if proxy:
        kwargs["proxy"] = proxy
    if storage_state:
        kwargs["storage_state"] = storage_state
    return await browser.new_context(**kwargs)


async def _maybe_open_contacts(page: Page, body: str, budget: "ContactOpenBudget | None") -> str:
    """Click "Відкрити контакти" once, if the page needs it and the budget
    (and work.ua's own live counter) allow it. Returns the page content to use
    from here on -- the original `body` unless a click actually happened and
    succeeded, in which case the freshly re-read content that should now
    contain the real phone number.

    Never raises: a missing button, a slow render, or work.ua changing this
    flow entirely must degrade to "no phone found this time", exactly like
    today's behaviour, not crash the whole search run over one resume.
    """
    if budget is None or budget.remaining <= 0:
        return body
    if _OPEN_CONTACTS_BUTTON_TEXT not in body:
        return body  # already open, or this candidate has no phone to open
    quota_match = _QUOTA_RE.search(body)
    if quota_match and int(quota_match.group(1)) <= 0:
        log.warning("scraper.workua.contact_quota_exhausted", remaining_today=0)
        budget.remaining = 0
        return body
    # 09.09.2026, found with a real employer session: `text=` resolves to the
    # wrapper div, whose centre is empty space next to the button -- the click
    # landed on nothing, Playwright reported success, the budget was spent and
    # the contact was never opened. Click the class-bound anchor first; keep
    # the text selector as a fallback in case work.ua renames the class.
    clicked = False
    for selector in (_OPEN_CONTACTS_SELECTOR, f"text={_OPEN_CONTACTS_BUTTON_TEXT}"):
        try:
            await page.click(selector, timeout=5_000)
            clicked = True
            break
        except Exception as e:
            # Not found/not clickable -- no request was sent, so no quota was
            # spent; try the next selector, then give up with the old body.
            log.info(
                "scraper.workua.open_contacts_click_failed",
                selector=selector,
                error=str(e),
            )
    if not clicked:
        return body
    budget.remaining -= 1
    # The reveal is an AJAX round-trip -- the number can take a few seconds to
    # land in the DOM. Re-read until it does rather than guessing one delay: a
    # too-early read is what made the first live test look like a failure even
    # though the contact HAD been opened (confirmed 09.09.2026).
    latest = body
    for _ in range(4):
        await _polite_delay(1.0, 2.0)
        try:
            latest = await page.content()
        except Exception:
            return latest
        if _PHONE_RE.search(latest):
            return latest
    return latest


async def _parse_resume_page(
    page: Page, url: str, *, budget: "ContactOpenBudget | None" = None
) -> ResumeListing | None:
    try:
        await _goto_with_retry(page, url)
    except Exception as e:
        log.warning("scraper.page_load_failed", url=url, error=str(e))
        return None

    name = await _text_or_none(page, "h1")
    desired = await _text_or_none(page, "h2")
    region_raw = await _text_or_none(page, '[data-key="region"]')
    region = normalize_region(region_raw or "")

    body = await page.content()
    body = await _maybe_open_contacts(page, body, budget)

    exp_match = _EXPERIENCE_RE.search(body)
    experience = int(exp_match.group(1)) if exp_match else None

    languages: list[str] = []
    for code, marker in [
        ("uk", "Українська"),
        ("ru", "Російська"),
        ("en", "Англійська"),
        ("pl", "Польська"),
        ("de", "Німецька"),
    ]:
        if marker in body:
            languages.append(code)

    # Hidden on public pages, and on an authenticated page too until contacts
    # are explicitly opened (see _maybe_open_contacts) -- either way, if it's
    # not in `body` by now there is genuinely nothing more we can do for this
    # candidate this run.
    phone_raw = None
    phone_match = _PHONE_RE.search(body)
    if phone_match:
        phone_raw = phone_match.group(1)

    return ResumeListing(
        work_ua_url=url,
        full_name=name,
        desired_position=desired,
        region=region or None,
        experience_years=experience,
        languages=languages,
        phone_e164=normalize_phone(phone_raw) if phone_raw else None,
        raw_html_snippet=None,
    )


async def _text_or_none(page: Page, selector: str) -> str | None:
    try:
        el = await page.query_selector(selector)
        if not el:
            return None
        text = await el.inner_text()
        return text.strip() if text else None
    except Exception:
        return None


async def scrape_resume_urls(urls: list[str]) -> list[ResumeListing]:
    s = get_settings()
    results: list[ResumeListing] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await _open_context(browser, s.workua_proxy_url or None)
        page = await ctx.new_page()
        try:
            for url in urls[: s.workua_scrape_daily_limit]:
                listing = await _parse_resume_page(page, url)
                if listing:
                    results.append(listing)
                await _polite_delay()
        finally:
            await ctx.close()
            await browser.close()
    return results


# ---------------------------------------------------------------------------
# Authenticated session (no password ever touches this code -- see module
# docstring). A human logs into work.ua once and hands us the resulting
# session; we only ever read and reuse it.
# ---------------------------------------------------------------------------

def _cookie_list_to_storage_state(data: Any) -> dict:
    """Accept either a real Playwright storage_state object ({"cookies": [...],
    "origins": [...]}) or a plain cookie array (the shape most browser cookie-
    export extensions produce) and normalize to the former. Raises ValueError
    on anything else so the caller can log a clear "bad file" warning instead
    of a confusing downstream Playwright error."""
    if isinstance(data, dict) and "cookies" in data:
        data.setdefault("origins", [])
        return data
    if isinstance(data, list):
        cookies = []
        for c in data:
            name, value = c.get("name"), c.get("value")
            if not name or value is None:
                continue
            same_site = c.get("sameSite") or "Lax"
            # Browser extensions export "no_restriction"/"lax"/"strict"; Playwright
            # wants "None"/"Lax"/"Strict".
            same_site = {"no_restriction": "None", "unspecified": "Lax"}.get(
                str(same_site).lower(), same_site
            )
            if same_site not in ("Strict", "Lax", "None"):
                same_site = "Lax"
            cookies.append({
                "name": name,
                "value": value,
                "domain": c.get("domain") or ".work.ua",
                "path": c.get("path") or "/",
                "expires": c.get("expirationDate", c.get("expires", -1)),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", True)),
                "sameSite": same_site,
            })
        return {"cookies": cookies, "origins": []}
    raise ValueError("unrecognized session file format (expected storage_state or cookie array)")


def load_session_state(path: Path) -> dict | None:
    """Read a saved work.ua session from disk.

    Never raises -- a missing or unreadable file just means "no cold sourcing
    this run", which every caller must be able to treat as a normal, silent
    skip on an unattended schedule, not a crash.
    """
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        state = _cookie_list_to_storage_state(raw)
        if not state.get("cookies"):
            log.warning("scraper.session_empty", path=str(path))
            return None
        return state
    except Exception as e:  # noqa: BLE001 — a bad session file must never crash a poll
        log.warning("scraper.session_load_failed", path=str(path), error=str(e))
        return None


def session_available(path: Path | None = None) -> bool:
    s = get_settings()
    return load_session_state(path or Path(s.workua_session_state_path)) is not None


# What work.ua renders for a visitor who is not signed in. Checked together with
# the cabinet markers rather than on their own: the employer landing page shows
# "Увійти" in its header while signed out and the company name plus «Мої
# вакансії» once the session is live, so it is the combination that is
# conclusive.
#
# Note for anyone testing this by hand: work.ua serves the anonymous page to a
# browser context that lacks the usual fingerprint, valid cookies or not. Build
# the context with `_open_context` or you will diagnose a dead session that is
# perfectly alive -- which is exactly what happened on 18.09.2026.
_SIGNED_OUT_MARKERS = (
    "Вхід для роботодавців",
    "Зареєструватися",
    "Увійти",
)
_SIGNED_IN_MARKERS = (
    "Вийти",
    "Мій кабінет",
    "Особистий кабінет",
    "Мої вакансії",
)


def _looks_signed_out(body: str) -> bool:
    """True when the page came back as the anonymous version of itself."""
    if any(m in body for m in _SIGNED_IN_MARKERS):
        return False
    return any(m in body for m in _SIGNED_OUT_MARKERS)


def _record_session_health(outcome: str) -> None:
    """Leave the keepalive's verdict where the daily report can read it.

    The report infers session health from `wsid`'s expiry, which the keepalive
    refreshes on every beat -- so the timestamp says "a beat happened", not "the
    beat found us signed in". The two only diverge once the session actually
    expires, which is precisely when the report matters most.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        path = Path(get_settings().workua_session_state_path).parent / "workua_session_health.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"outcome": outcome, "at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001 — a breadcrumb must never break the heartbeat
        log.warning("scraper.workua.session_health_write_failed", error=str(e)[:120])


async def refresh_session(url: str = "https://www.work.ua/employer/") -> str:
    """Extend the saved employer session and persist the refreshed cookies.

    `wsid` is a sliding ~30-minute window work.ua extends on each request, and
    nothing else in the system touches this session (warm responses come from
    work.ua's API, not the browser), so without this heartbeat a freshly
    exported session is dead within the hour and cold sourcing silently skips
    every run until a human exports again.

    Returns a short status string for the log rather than raising: this runs
    unattended on a timer and must never take the scheduler down.
    """
    s = get_settings()
    path = Path(s.workua_session_state_path)
    state = load_session_state(path)
    if state is None:
        return "no_session"
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await _open_context(browser, s.workua_proxy_url or None, storage_state=state)
            page = await ctx.new_page()
            try:
                await _goto_with_retry(page, url)
                body = await page.content()
                if _looks_challenged(body):
                    # Do not persist cookies from a challenge page and do not
                    # retry harder -- that is how a soft block becomes a hard one.
                    _record_session_health("challenged")
                    return "challenged"
                # `body` above is page source; the cabinet strings live in
                # inert markup there even when signed out. Judge the rendered
                # text — the same thing a person looking at the page would see.
                try:
                    visible = await page.inner_text("body")
                except Exception:  # noqa: BLE001 — fall back to the source
                    visible = body
                if _looks_signed_out(visible):
                    # Sessions do expire, and when this one does, saving here
                    # would overwrite the stored cookies with an anonymous jar,
                    # making a recoverable expiry unrecoverable -- while "ok"
                    # would hide from the daily report the one thing a human has
                    # to act on (export cookies again).
                    log.warning("scraper.workua.session_signed_out", url=url)
                    _record_session_health("logged_out")
                    return "logged_out"
                refreshed = await ctx.storage_state()
                if refreshed.get("cookies"):
                    path.write_text(
                        json.dumps(refreshed, ensure_ascii=False), encoding="utf-8"
                    )
                _record_session_health("ok")
                return "ok"
            finally:
                await ctx.close()
                await browser.close()
    except Exception as e:  # noqa: BLE001 -- a heartbeat must never crash the timer
        log.warning("scraper.workua.session_keepalive_error", error=str(e)[:160])
        return "error"


# ---------------------------------------------------------------------------
# Authenticated search -- the cold-sourcing half. Requires a session (above);
# without one this whole section is unreachable in practice, since there
# would be no phone numbers to collect anyway.
# ---------------------------------------------------------------------------

# Matches a resume detail link by URL SHAPE rather than a CSS class or section
# selector — work.ua's markup changes far more often than its URL scheme, and
# a shape match survives a redesign that a class name would not (the same
# reasoning already used for the phone-number regex above).
_RESUME_LINK_RE = re.compile(r'href="(/resumes/\d+[^"?#]*)"')


def _extract_resume_urls(html: str) -> list[str]:
    seen: list[str] = []
    for path in _RESUME_LINK_RE.findall(html):
        url = f"https://www.work.ua{path}"
        if url not in seen:
            seen.append(url)
    return seen


def _search_url(query: str, page: int = 1) -> str:
    """Best-effort work.ua resume-search URL for a free-text query, nationwide.

    No city segment on purpose: region is enforced afterwards via
    `filter_by_region()` against each resume's own stated location, which is
    already implemented and reliable, rather than maintaining a second mapping
    from Ukrainian oblast names to work.ua's city-slug scheme here.

    CAVEAT: the /resumes-<slug>/ path shape was confirmed reachable without a
    session on 05.09.2026 (loads a real resume-search page), but the exact
    pagination query param was NOT re-verified against a live, authenticated
    search — automated browsing of work.ua beyond that one check was blocked
    by this session's own safety tooling (scraping-adjacent access). Re-check
    this once a real session file exists and adjust `page=` below if work.ua
    actually uses a different param name.
    """
    from urllib.parse import quote

    slug = quote(query.strip().replace(" ", "+"), safe="+")
    url = f"https://www.work.ua/resumes-{slug}/"
    return url if page <= 1 else f"{url}?page={page}"


async def _search_one_query(page: Page, query: str, *, max_results: int) -> list[str]:
    urls: list[str] = []
    for page_num in range(1, 6):  # hard stop — never paginate forever on one query
        if len(urls) >= max_results:
            break
        try:
            await _goto_with_retry(page, _search_url(query, page=page_num))
        except Exception as e:
            log.warning("scraper.search_page_failed", query=query, page=page_num, error=str(e))
            break
        body = await page.content()
        if _looks_challenged(body):
            # Keep hitting it and the block only gets deeper; give up on this
            # query and let the next scheduled run try from a calmer state.
            log.warning(
                "scraper.workua.bot_challenge",
                query=query, page=page_num,
                note="work.ua served its bot-check interstitial -- no results this run",
            )
            break
        found = _extract_resume_urls(body)
        if not found:
            break
        for u in found:
            if u not in urls:
                urls.append(u)
        await _human_delay()
    return urls[:max_results]


async def search_resumes(
    *,
    queries: list[str],
    max_per_query: int | None = None,
    total_limit: int | None = None,
    session_state_path: Path | None = None,
) -> list[ResumeListing]:
    """Search the work.ua resume database for the given free-text queries and
    return region-filtered listings with phone numbers where the session
    reveals them.

    Graceful by design: returns [] and logs once, rather than raising, when no
    session file is on disk. This runs unattended on a schedule — a missing or
    stale session must never take down a poll cycle.
    """
    s = get_settings()
    path = session_state_path or Path(s.workua_session_state_path)
    state = load_session_state(path)
    if state is None:
        _record_cold_sourcing_run("no_session")
        log.warning(
            "scraper.cold_sourcing.no_session",
            path=str(path),
            note="no saved work.ua session -- skipping cold sourcing this run",
        )
        return []

    max_per_query = max_per_query or s.workua_cold_sourcing_max_per_query
    total_limit = total_limit or s.workua_scrape_daily_limit
    budget = ContactOpenBudget(remaining=max(0, s.workua_max_contact_opens_per_run))

    urls: list[str] = []
    results: list[ResumeListing] = []
    challenged = False
    last_title = ""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await _open_context(browser, s.workua_proxy_url or None, storage_state=state)
        page = await ctx.new_page()
        try:
            # Warm-up: land in the cabinet first, like a person opening work.ua
            # before searching, instead of appearing straight on a resume-search
            # URL. The cabinet is served normally even while search is
            # challenged, so this also tells us early whether the session works.
            try:
                await _goto_with_retry(page, "https://www.work.ua/employer/")
                await _human_delay()
            except Exception as e:  # noqa: BLE001 -- warm-up is best effort
                log.info("scraper.workua.warmup_failed", error=str(e)[:120])

            for query in queries:
                if len(urls) >= total_limit:
                    break
                found = await _search_one_query(page, query, max_results=max_per_query)
                if not found:
                    body = await page.content()
                    challenged = challenged or _looks_challenged(body)
                    # Keep the page's own title: a zero that cannot be explained
                    # afterwards is how a block gets mistaken for a quiet day.
                    try:
                        last_title = await page.title()
                    except Exception:
                        last_title = ""
                for u in found:
                    if u not in urls and len(urls) < total_limit:
                        urls.append(u)
                await _human_delay()

            for url in urls:
                listing = await _parse_resume_page(page, url, budget=budget)
                if listing:
                    results.append(listing)
                await _human_delay()
        finally:
            # work.ua's session cookie (`wsid`) is a sliding ~30-minute window
            # the server refreshes on every request. Write the context's
            # cookies back so the saved session renews itself; without this a
            # fresh export from the recruiter goes stale within the hour and
            # cold sourcing stops silently (it would just log no_session).
            try:
                # A challenged run ends on Cloudflare's page, whose cookies are
                # not the employer session -- saving those over a good session
                # would quietly downgrade it.
                refreshed = {} if challenged else await ctx.storage_state()
                if refreshed.get("cookies"):
                    path.write_text(
                        json.dumps(refreshed, ensure_ascii=False), encoding="utf-8"
                    )
                    log.info("scraper.workua.session_refreshed", path=str(path))
            except Exception as e:  # noqa: BLE001 -- never fail a run over this
                log.warning("scraper.workua.session_refresh_failed", error=str(e))
            await ctx.close()
            await browser.close()

    filtered = filter_by_region(results)
    _record_cold_sourcing_run(
        "challenged" if challenged else "ok",
        last_page_title=last_title,
        urls_found=len(urls),
        with_phone=sum(1 for r in results if r.phone_e164),
        after_region_filter=len(filtered),
        contact_opens_spent=s.workua_max_contact_opens_per_run - budget.remaining,
    )
    log.info(
        "scraper.cold_sourcing.search_done",
        queries=len(queries), urls_found=len(urls),
        with_phone=sum(1 for r in results if r.phone_e164),
        after_region_filter=len(filtered),
        contact_opens_spent=s.workua_max_contact_opens_per_run - budget.remaining,
        contact_opens_remaining=budget.remaining,
    )
    return filtered


def filter_by_region(listings: list[ResumeListing]) -> list[ResumeListing]:
    s = get_settings()
    allowed = s.regions_allowed
    blocked = s.regions_blocked
    return [r for r in listings if r.region and is_region_allowed(r.region, allowed, blocked)]


async def run_once(urls: list[str], out_path: Path | None = None) -> list[dict[str, Any]]:
    raw = await scrape_resume_urls(urls)
    filtered = filter_by_region(raw)
    out = [asdict(r) for r in filtered]
    if out_path:
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("scraper.saved", path=str(out_path), kept=len(out), total=len(raw))
    return out


if __name__ == "__main__":
    import sys

    urls = sys.argv[1:] or []
    asyncio.run(run_once(urls, Path("scraped_resumes.json")))
