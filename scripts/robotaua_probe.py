"""One-off recon: log into robota.ua employer cabinet, record the XHR calls the
SPA makes, and dump enough of the DOM/localStorage to write a real client.

Run:  docker exec deploy-api-1 python /probe/robotaua_probe.py
Output: /state/robotaua_probe/ (network.json, storage.json, *.html)
Never prints the password; tokens are truncated.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

OUT = Path("/state/robotaua_probe")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


async def main() -> None:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    # The long-running containers were started before these vars existed, so fall
    # back to a copy of .env dropped next to the script.
    fallback = Path("/tmp/ra.env")
    if fallback.exists():
        for line in fallback.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("ROBOTAUA_") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    email = os.environ["ROBOTAUA_EMPLOYER_EMAIL"]
    password = os.environ["ROBOTAUA_EMPLOYER_PASSWORD"]

    net: list[dict] = []

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    ctx = await browser.new_context(
        user_agent=UA,
        viewport={"width": 1440, "height": 900},
        locale="uk-UA",
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )

    async def on_response(resp):
        url = resp.url
        if "robota.ua" not in url:
            return
        if not any(k in url for k in ("api", "graphql", "Login", "employer")):
            return
        rec = {
            "method": resp.request.method,
            "url": url,
            "status": resp.status,
            "type": resp.request.resource_type,
        }
        try:
            if resp.request.method == "POST":
                rec["post"] = (resp.request.post_data or "")[:600]
        except Exception:
            pass
        try:
            ct = (resp.headers.get("content-type") or "")
            if "json" in ct and resp.request.resource_type in ("xhr", "fetch"):
                body = await resp.text()
                rec["body_head"] = body[:1500]
                rec["body_len"] = len(body)
        except Exception as e:
            rec["body_err"] = str(e)[:120]
        net.append(rec)

    ctx.on("response", lambda r: asyncio.ensure_future(on_response(r)))

    page = await ctx.new_page()
    step: dict[str, str] = {}

    async def snap(name: str) -> None:
        step[name] = page.url
        (OUT / f"{name}.html").write_text(await page.content(), encoding="utf-8")
        try:
            await page.screenshot(path=str(OUT / f"{name}.png"), full_page=False)
        except Exception:
            pass

    await page.goto("https://robota.ua/auth/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    await snap("01_login")

    # Fill whatever the login form actually exposes.
    filled = []
    for sel in (
        "input[name='email']",
        "input[type='email']",
        "input#email",
        "input[formcontrolname='email']",
        "input[placeholder*='ошта']",
        "input[placeholder*='mail']",
    ):
        try:
            if await page.locator(sel).count():
                await page.locator(sel).first.fill(email)
                filled.append(sel)
                break
        except Exception:
            continue
    for sel in (
        "input[name='password']",
        "input[type='password']",
        "input[formcontrolname='password']",
    ):
        try:
            if await page.locator(sel).count():
                await page.locator(sel).first.fill(password)
                filled.append(sel)
                break
        except Exception:
            continue
    step["filled_selectors"] = ",".join(filled)

    for sel in (
        "button[type='submit']",
        "button:has-text('Увійти')",
        "button:has-text('Войти')",
        "alliance-login button",
    ):
        try:
            if await page.locator(sel).count():
                await page.locator(sel).first.click()
                step["submit_selector"] = sel
                break
        except Exception:
            continue

    await page.wait_for_timeout(8000)
    await snap("02_after_login")

    for name, url in (
        ("03_vacancies", "https://robota.ua/my/vacancies"),
        ("04_candidates", "https://robota.ua/my/vacancies/11277559/candidates"),
    ):
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(7000)
            await snap(name)
        except Exception as e:
            step[name] = f"ERR {e}"

    try:
        storage = await page.evaluate(
            "() => ({ local: Object.fromEntries(Object.entries(localStorage)),"
            " session: Object.fromEntries(Object.entries(sessionStorage)) })"
        )
    except Exception as e:
        storage = {"err": str(e)}

    def trunc(d):
        if isinstance(d, dict):
            return {k: trunc(v) for k, v in d.items()}
        if isinstance(d, str):
            return d[:160] + ("…" if len(d) > 160 else "")
        return d

    (OUT / "storage.json").write_text(
        json.dumps(trunc(storage), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "network.json").write_text(
        json.dumps(net, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "steps.json").write_text(
        json.dumps(step, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cookies = await ctx.cookies()
    (OUT / "cookies.json").write_text(
        json.dumps([c["name"] for c in cookies], ensure_ascii=False), encoding="utf-8"
    )

    print("steps:", json.dumps(step, ensure_ascii=False))
    print("network records:", len(net))
    for r in net[:40]:
        print(r["status"], r["method"], r["url"][:130])

    await ctx.close()
    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
