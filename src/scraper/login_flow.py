"""work.ua authenticated session — employer login + base-resume search.

DANGER: ToS-grey area. Use only with a residential proxy + 1 employer account +
human-paced timing. A bot signature gets the account banned within hours.

Pattern:
1. Persistent context (cookies survive restarts) — avoids re-login every run
2. UA viewport, real-Chrome UA string, locale uk-UA, tz Europe/Kyiv
3. Random delays 1.5-4.5s between every action
4. Daily search cap = settings.workua_scrape_daily_limit
5. Backup account in env vars — switch over on ban detection
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import asdict
from pathlib import Path

import structlog
from playwright.async_api import BrowserContext, Page, async_playwright

from src.common.settings import get_settings

from .workua import ResumeListing, filter_by_region

log = structlog.get_logger()

LOGIN_URL = "https://www.work.ua/employer/"
SEARCH_URL = "https://www.work.ua/employer/resumes/"

STORAGE_STATE_PATH = Path(".cache/workua_storage.json")


async def _delay(min_s: float = 1.5, max_s: float = 4.5) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _ensure_logged_in(context: BrowserContext, email: str, password: str) -> Page:
    page = await context.new_page()
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await _delay(0.5, 1.5)

    # Detect logged-in state by absence of login button — selectors are fragile,
    # tune after first manual run.
    if await page.query_selector('text="Вийти"'):
        return page

    # Fill credentials with human delays
    email_el = await page.wait_for_selector("input[name=email]", timeout=10_000)
    await email_el.fill(email, timeout=5_000)
    await _delay(0.6, 1.3)
    pwd_el = await page.wait_for_selector("input[name=password]", timeout=10_000)
    await pwd_el.fill(password, timeout=5_000)
    await _delay(0.5, 1.2)
    submit = await page.wait_for_selector('button[type=submit]', timeout=10_000)
    await submit.click()
    await page.wait_for_load_state("networkidle", timeout=15_000)
    await _delay(2.0, 4.0)

    if not await page.query_selector('text="Вийти"'):
        log.error("workua.login_failed")
        raise RuntimeError("work.ua login failed")
    await context.storage_state(path=str(STORAGE_STATE_PATH))
    log.info("workua.login_ok")
    return page


async def search_resumes(
    *,
    query: str,
    pages_to_scan: int = 1,
) -> list[ResumeListing]:
    s = get_settings()
    if not (s.workua_employer_email and s.workua_employer_password):
        log.warning("workua.no_credentials")
        return []

    STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    results: list[ResumeListing] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        storage = str(STORAGE_STATE_PATH) if STORAGE_STATE_PATH.exists() else None
        context = await browser.new_context(
            storage_state=storage,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="uk-UA",
            timezone_id="Europe/Kyiv",
            proxy={"server": s.workua_proxy_url} if s.workua_proxy_url else None,
        )
        try:
            page = await _ensure_logged_in(
                context, s.workua_employer_email, s.workua_employer_password
            )
            for page_num in range(1, pages_to_scan + 1):
                await _delay(2.0, 5.0)
                url = f"{SEARCH_URL}?search={query}&page={page_num}"
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                resume_anchors = await page.query_selector_all('a[href^="/resumes/"]')
                hrefs = []
                for a in resume_anchors:
                    href = await a.get_attribute("href")
                    if href and "/resumes/" in href:
                        hrefs.append("https://www.work.ua" + href)
                # We're polite: stop early if we hit per-day cap
                hrefs = hrefs[: max(0, s.workua_scrape_daily_limit - len(results))]
                from .workua import _parse_resume_page  # local import — same context

                for url2 in hrefs:
                    listing = await _parse_resume_page(page, url2)
                    if listing:
                        results.append(listing)
                    await _delay()
                    if len(results) >= s.workua_scrape_daily_limit:
                        break
                if len(results) >= s.workua_scrape_daily_limit:
                    break
        finally:
            await context.close()
            await browser.close()

    filtered = filter_by_region(results)
    log.info("workua.search_done", query=query, raw=len(results), kept=len(filtered))
    return filtered
