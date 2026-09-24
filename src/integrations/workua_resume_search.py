"""Cold sourcing over work.ua's official API instead of the browser.

Why this exists (10.09.2026): Cloudflare blocks the resume-search pages for this
server's IP, and the obvious answer — buy a residential proxy — turned out to
solve the wrong problem. Probing the API showed the real ceiling: of every 20
resumes a search matches, this employer account can open only about 1-4. The
rest come back as "немає можливості відкрити контакти ... доступ до бази
кандидатів потрібного регіону". A proxy would have bought a clear view of
candidates we still could not contact.

The API has no such obstacle: it is the same endpoint the response poller uses
every five minutes without a single block, it returns exactly the fields the
portrait filter needs (name, birth date, region, positions) for free, and it
hands over a phone only on an explicit second call — which is the one step that
costs a paid credit.

So the order here is: search wide and free, discard locally, and spend the day's
credits only on the people who survived. The browser scraper stays in place as a
fallback for the day work.ua changes this API.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime

import structlog

from typing import Awaitable, Callable

from src.common.phone import normalize_phone
from src.common.regions import is_region_allowed
from src.common.settings import get_settings
from src.integrations.workua_api import WorkUaClient
from src.scraper.workua import ResumeListing

log = structlog.get_logger()

_RESUME_URL = "https://www.work.ua/resumes/{resume_id}/"
# work.ua answers a page of 20 with only the accessible ones in `result`, so a
# handful of pages is enough to fill a day's budget without hammering anything.
_MAX_PAGES_PER_QUERY = 3  # nationwide fallback only; per-city paging is a setting

# work.ua's town dictionary is in Russian; these are the main cities of the
# oblasts we hire in, which is where the resumes are. Searching by town id keeps
# every result inside an allowed region, so nothing is fetched only to be thrown
# away by the region filter afterwards.
_MAIN_CITY_BY_OBLAST = {
    "Волинська": "Луцк",
    "Дніпропетровська": "Днепр",
    "Житомирська": "Житомир",
    "Закарпатська": "Ужгород",
    "Івано-Франківська": "Ивано-Франковск",
    "Львівська": "Львов",
    "Одеська": "Одесса",
    "Рівненська": "Ровно",
    "Тернопільська": "Тернополь",
    "Хмельницька": "Хмельницкий",
    "Черкаська": "Черкассы",
    "Чернівецька": "Черновцы",
}
_PAGE_LIMIT = 20
_PAUSE_BETWEEN_CALLS_SEC = 1.5


def _age_from_birth_date(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        born = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _positions_text(raw: dict) -> str | None:
    """What the person is looking for, as one string for the matcher."""
    name = (raw.get("name") or "").strip()
    positions = raw.get("positions")
    if isinstance(positions, list) and positions:
        joined = ", ".join(str(p).strip() for p in positions if str(p).strip())
        if name and name not in joined:
            joined = f"{name}, {joined}"
        return joined or None
    return name or None


def _extract_phone(payload: dict) -> str | None:
    """Pull a phone out of GET /resume, whatever key it arrives under.

    The response nests under `result` and the contact field has moved before;
    scanning the values is more durable than guessing the key, and there is
    nothing else phone-shaped in the payload.
    """
    import re

    blob = payload.get("result") if isinstance(payload.get("result"), (dict, list)) else payload
    import json as _json

    text = _json.dumps(blob, ensure_ascii=False)
    m = re.search(r"(?:\+?38)?0\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}", text)
    return m.group(0) if m else None


def _passes_region(region: str | None) -> bool:
    if not region:
        return False
    s = get_settings()
    return is_region_allowed(region, s.regions_allowed, s.regions_blocked)


async def _allowed_city_ids(client: WorkUaClient) -> list[tuple[str, int]]:
    """work.ua town ids for the oblasts we hire in, as (city, id) pairs.

    Resolved from work.ua's own dictionary rather than hardcoded, so a renamed
    or renumbered town does not silently drop a whole region. Returns an empty
    list on any failure, and the caller falls back to a nationwide search.

    The order rotates daily. Each run stops as soon as it has enough survivors,
    so a fixed order would mean querying the same first cities every morning and
    re-finding the same people; rotating spreads the coverage over a week.
    """
    s = get_settings()
    wanted = {
        _MAIN_CITY_BY_OBLAST[o]: o
        for o in s.regions_allowed
        if o in _MAIN_CITY_BY_OBLAST
    }
    if not wanted:
        return []
    try:
        towns = await client.get_dictionary("town")
    except Exception as e:  # noqa: BLE001 — fall back to nationwide
        log.warning("cold_sourcing.api.town_dictionary_failed", error=str(e)[:160])
        return []

    found: dict[str, int] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            name = str(node.get("name") or node.get("title") or "").strip()
            rid = node.get("id") or node.get("rid")
            if name in wanted and rid is not None and name not in found:
                try:
                    found[name] = int(rid)
                except (TypeError, ValueError):
                    pass
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(towns)
    missing = sorted(set(wanted) - set(found))
    if missing:
        log.info("cold_sourcing.api.cities_unresolved", cities=missing)

    ordered = sorted(found.items(), key=lambda kv: kv[0])
    if ordered:
        shift = date.today().toordinal() % len(ordered)
        ordered = ordered[shift:] + ordered[:shift]
    return ordered


def _provisional_listing(raw: dict) -> ResumeListing:
    """The candidate as the free search describes them — everything except the
    phone, which is the only part that costs a credit."""
    rid = str(raw.get("resume_id") or raw.get("id") or "")
    name = " ".join(
        p for p in ((raw.get("first_name") or "").strip(), (raw.get("last_name") or "").strip()) if p
    ) or None
    return ResumeListing(
        work_ua_url=_RESUME_URL.format(resume_id=rid),
        full_name=name,
        desired_position=_positions_text(raw),
        region=(raw.get("region") or "").split(",")[0].strip() or None,
        experience_years=None,
        languages=[],
        phone_e164=None,
        raw_html_snippet=None,
    )


def record_cold_sourcing_counts(counts: dict) -> None:
    """Write the run breadcrumb the daily report reads.

    Kept here rather than inline so a cycle can hand over accumulated totals
    instead of whatever its last vacancy happened to find.
    """
    try:
        from src.scraper.workua import _record_cold_sourcing_run

        _record_cold_sourcing_run("ok", source="api", **counts)
    except Exception as e:  # noqa: BLE001 — a breadcrumb must never break a run
        log.warning("cold_sourcing.api.breadcrumb_failed", error=str(e)[:120])


async def search_resumes_via_api(
    *,
    queries: list[str],
    total_limit: int,
    open_budget: int,
    age_from: int = 22,
    age_to: int = 42,
    period: int = 3,
    prescreen: "Callable[[ResumeListing], Awaitable[bool]] | None" = None,
    client: WorkUaClient | None = None,
    tally: dict | None = None,
) -> list[ResumeListing]:
    """Free search, local filtering, then paid contact opens for survivors only.

    `open_budget` is the hard cap on paid calls — the account's daily contact
    allowance. Nothing here can exceed it, however many queries are passed in.
    """
    own_client = client is None
    client = client or WorkUaClient()
    seen_ids: set[str] = set()
    survivors: list[dict] = []

    s = get_settings()
    regions: list[tuple[str, int | None]] = [("вся Україна", None)]
    max_pages = _MAX_PAGES_PER_QUERY
    if s.workua_cold_sourcing_by_region:
        resolved = await _allowed_city_ids(client)
        if resolved:
            regions = [(city, rid) for city, rid in resolved]
            max_pages = max(1, s.workua_cold_sourcing_pages_per_query)
            log.info(
                "cold_sourcing.api.regions",
                cities=[c for c, _ in regions], count=len(regions), pages_each=max_pages,
            )
    pages_left = max(1, s.workua_cold_sourcing_max_pages_per_run)

    try:
        for query in queries:
            if len(survivors) >= total_limit or pages_left <= 0:
                break
            for city, region_id in regions:
                if len(survivors) >= total_limit or pages_left <= 0:
                    break
                for page in range(1, max_pages + 1):
                    if len(survivors) >= total_limit or pages_left <= 0:
                        break
                    pages_left -= 1
                    try:
                        data = await client.search_resumes(
                            search=query,
                            region_id=region_id,
                            with_phone=False,  # free listing; contacts come later
                            age_from=age_from,
                            age_to=age_to,
                            period=period,
                            limit=_PAGE_LIMIT,
                            page=page,
                        )
                    except Exception as e:  # noqa: BLE001 — one query must not sink the run
                        log.warning(
                            "cold_sourcing.api.search_failed",
                            query=query, city=city, page=page, error=str(e)[:200],
                        )
                        break

                    items = data.get("result") or []
                    blocked = len(data.get("errors") or [])
                    log.info(
                        "cold_sourcing.api.page",
                        query=query, city=city, page=page, accessible=len(items),
                        blocked_by_access=blocked, all_count=data.get("allCount"),
                    )
                    # An openable resume is rare (~3%), so a page of nothing but
                    # blocked ones is normal and must not end the sweep — only a
                    # genuinely empty page means we have run off the end.
                    if not items and not blocked:
                        break

                    for raw in items:
                        rid = str(raw.get("resume_id") or raw.get("id") or "")
                        if not rid or rid in seen_ids:
                            continue
                        seen_ids.add(rid)
                        region = (raw.get("region") or "").split(",")[0].strip()
                        if not _passes_region(region):
                            log.info(
                                "cold_sourcing.api.region_rejected",
                                region=region, resume_id=rid, searched_city=city,
                            )
                            continue
                        if prescreen is not None:
                            # Everything that can reject this person must happen
                            # here, while looking at them is still free.
                            try:
                                keep = await prescreen(_provisional_listing(raw))
                            except Exception as e:  # noqa: BLE001 — a broken screen must not spend money
                                log.warning(
                                    "cold_sourcing.api.prescreen_failed",
                                    resume_id=rid, error=str(e)[:160],
                                )
                                keep = False
                            if not keep:
                                continue
                        survivors.append(raw)
                        if len(survivors) >= total_limit:
                            break
                    await asyncio.sleep(_PAUSE_BETWEEN_CALLS_SEC)

        log.info(
            "cold_sourcing.api.filtered",
            survivors=len(survivors), open_budget=open_budget, queries=len(queries),
        )

        listings: list[ResumeListing] = []
        for raw in survivors[:open_budget]:
            rid = str(raw.get("resume_id") or raw.get("id"))
            try:
                full = await client.get_resume(int(rid))
            except Exception as e:  # noqa: BLE001 — a failed open costs us this one candidate
                log.warning("cold_sourcing.api.open_failed", resume_id=rid, error=str(e)[:200])
                continue
            phone_raw = _extract_phone(full or {})
            phone = normalize_phone(phone_raw) if phone_raw else None
            if not phone:
                log.info("cold_sourcing.api.no_phone", resume_id=rid)
                continue
            listing = _provisional_listing(raw)
            listing.phone_e164 = phone
            listings.append(listing)
            await asyncio.sleep(_PAUSE_BETWEEN_CALLS_SEC)

        log.info(
            "cold_sourcing.api.done",
            opened=len(listings), survivors=len(survivors), age_from=age_from, age_to=age_to,
        )
        # Counts for the daily report. A cycle calls this once per vacancy, so
        # they are added to a tally the caller owns and written out once at the
        # end -- writing here made each vacancy overwrite the previous one's
        # numbers, and the report showed the smallest of them.
        counts = {
            "urls_found": len(seen_ids),
            "survivors": len(survivors),
            "with_phone": len(listings),
            "after_region_filter": len(survivors),
            "contact_opens_spent": len(listings),
        }
        if tally is None:
            record_cold_sourcing_counts(counts)
        else:
            for k, v in counts.items():
                tally[k] = tally.get(k, 0) + v
        return listings
    finally:
        if own_client:
            await client.aclose()
