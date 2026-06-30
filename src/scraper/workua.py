"""work.ua scraper — public resume pages.

NOTE: This is a prototype against PUBLIC resume pages. For logged-in employer access
(which exposes phone numbers and the full resume database search), extend the
`login()` step and respect rate limits (<30 searches/day, randomized delays, 1 account).
The Terms of Service of work.ua forbid automated access. Use a residential proxy and a
backup employer account in case of bans.
"""
from __future__ import annotations

import asyncio
import json
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


_EXPERIENCE_RE = re.compile(r"(\d+)\s*(?:рок|year|год)", re.IGNORECASE)


async def _polite_delay(min_s: float = 1.5, max_s: float = 4.5) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _open_context(browser: Browser, proxy_url: str | None) -> BrowserContext:
    kwargs: dict[str, object] = {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1440, "height": 900},
        "locale": "uk-UA",
        "timezone_id": "Europe/Kyiv",
    }
    if proxy_url and proxy_url.strip():
        kwargs["proxy"] = {"server": proxy_url.strip()}
    return await browser.new_context(**kwargs)


async def _parse_resume_page(page: Page, url: str) -> ResumeListing | None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
    except Exception as e:
        log.warning("scraper.page_load_failed", url=url, error=str(e))
        return None

    name = await _text_or_none(page, "h1")
    desired = await _text_or_none(page, "h2")
    region_raw = await _text_or_none(page, '[data-key="region"]')
    region = normalize_region(region_raw or "")

    body = await page.content()
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

    # phone is hidden on public pages — only visible to logged-in employers
    phone_raw = None
    phone_match = re.search(r"\+?380[\d\s\-()]{9,}", body)
    if phone_match:
        phone_raw = phone_match.group(0)

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
