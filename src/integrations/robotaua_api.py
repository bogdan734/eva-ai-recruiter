"""robota.ua Playwright scraper — logs into employer cabinet, pulls only
"Відгуки" (Applications) responses. Skips "Відкрив контакти" (contact
opens) and "Додав в обране" (favourites) which are not real applications.

Public partner API from robota.ua requires a written agreement; this
scraper is the practical workaround until a token arrives.

Auth:
  - ROBOTAUA_EMPLOYER_EMAIL, ROBOTAUA_EMPLOYER_PASSWORD in .env
  - Session cookie is persisted under `.cache/robotaua_state.json` so
    subsequent runs skip the login flow.

Response shape (normalized):
  {
    "id": <int>,             # robota.ua response id
    "vacancy_id": <int>,
    "full_name": <str>,
    "phone": <str>,
    "email": <str | None>,
    "region": <str | None>,
    "resume_url": <str | None>,
    "applied_at": <ISO 8601 str>,
    "raw": {...},
  }
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import structlog

from src.common.settings import get_settings

log = structlog.get_logger()

STATE_PATH = Path(".cache/robotaua_state.json")
LOGIN_URL = "https://robota.ua/auth/login"
CABINET_URL = "https://robota.ua/employer/notifications"

# Robota.ua candidate-list tabs. We want only the first.
TAB_APPLICATIONS = "applies"
SKIP_TABS = {"opened_contacts", "favourites", "viewed"}


class RobotaUaError(RuntimeError):
    pass


class RobotaUaAuthError(RobotaUaError):
    pass


class RobotaUaClient:
    """Playwright-driven client scoped to reading applications.

    Runs headless. Costs ~2-5 sec per poll (login already cached).
    Not suitable for high-volume scraping — designed for 15-30 min cadence.
    """

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        allowed_vacancy_ids: set[int] | None = None,
    ) -> None:
        s = get_settings()
        self._email = email or s.robotaua_employer_email
        self._password = password or s.robotaua_employer_password
        self._allowed = allowed_vacancy_ids or set()
        if not self._email or not self._password:
            raise RobotaUaAuthError(
                "ROBOTAUA_EMPLOYER_EMAIL / ROBOTAUA_EMPLOYER_PASSWORD not set"
            )

    async def _launch(self):
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        storage = str(STATE_PATH) if STATE_PATH.exists() else None
        ctx = await browser.new_context(storage_state=storage)
        return pw, browser, ctx

    async def _login(self, ctx) -> None:
        page = await ctx.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        # robota.ua login form: input[name=email] + input[name=password] + submit
        # NOTE: selectors change occasionally. Update when the form breaks.
        await page.fill("input[name='email']", self._email)
        await page.fill("input[name='password']", self._password)
        await page.click("button[type='submit']")
        await page.wait_for_load_state("networkidle")
        if "/auth/" in page.url:
            content = await page.content()
            raise RobotaUaAuthError(f"login failed, still on {page.url}")
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(STATE_PATH))
        await page.close()

    async def list_new_applications(
        self, since_id: int | None = None
    ) -> list[dict]:
        """Scrape the applications tab; return only responses newer than since_id."""
        pw, browser, ctx = await self._launch()
        try:
            page = await ctx.new_page()
            await page.goto(CABINET_URL, wait_until="domcontentloaded")
            if "/auth/" in page.url:
                await page.close()
                await self._login(ctx)
                page = await ctx.new_page()
                await page.goto(CABINET_URL, wait_until="domcontentloaded")

            # Click the "Відгуки" tab explicitly (not opened_contacts / favourites)
            try:
                await page.get_by_role("tab", name=re.compile(r"^Відгуки", re.I)).click()
            except Exception:
                # Fallback selector variants
                await page.click("button:has-text('Відгуки')")
            await page.wait_for_load_state("networkidle")

            # Response cards — robota.ua uses <div class="notification-card"> style
            # containers. We extract application_id + vacancy_id from data attrs.
            cards = await page.locator(".notification-card").all()
            out: list[dict] = []
            for card in cards:
                try:
                    parsed = await self._parse_card(card)
                except Exception as e:
                    log.warning("robotaua.card_parse_failed", error=str(e))
                    continue
                if not parsed:
                    continue
                if parsed.get("kind") in SKIP_TABS:
                    continue
                if since_id and parsed.get("id", 0) <= since_id:
                    continue
                if self._allowed and parsed.get("vacancy_id") not in self._allowed:
                    continue
                out.append(parsed)
            return out
        finally:
            await ctx.close()
            await browser.close()
            await pw.stop()

    async def _parse_card(self, card) -> dict | None:
        # Selectors are placeholders — refresh once the real DOM is inspected
        # with the client's active vacancy loaded.
        raw_id = await card.get_attribute("data-id")
        vacancy_id = await card.get_attribute("data-vacancy-id")
        kind = await card.get_attribute("data-kind")
        name = (
            await card.locator(".candidate-name").first.inner_text()
        ).strip() if await card.locator(".candidate-name").count() else None
        phone_el = card.locator(".candidate-phone")
        phone = (
            await phone_el.first.inner_text()
        ).strip() if await phone_el.count() else None
        if not raw_id or not name:
            return None
        return {
            "id": int(raw_id),
            "vacancy_id": int(vacancy_id) if vacancy_id else None,
            "kind": kind or TAB_APPLICATIONS,
            "full_name": name,
            "phone": phone,
            "email": None,
            "region": None,
            "resume_url": None,
        }


def parse_application(payload: dict) -> dict:
    return {
        "external_id": str(payload.get("id")),
        "full_name": payload.get("full_name") or "Кандидат robota.ua",
        "phone_raw": payload.get("phone") or "",
        "email": payload.get("email"),
        "region_raw": payload.get("region"),
        "desired_position": None,
        "vacancy_external_id": (
            str(payload.get("vacancy_id"))
            if payload.get("vacancy_id") is not None
            else None
        ),
    }
