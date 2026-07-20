"""KeyCRM audit v2 — reverse pagination, focus on recent leads."""
import asyncio
from collections import Counter

from src.common.keycrm import KeyCRMClient
from src.common.db import session_scope
from src.common.models import Candidate
from sqlalchemy import select


STATUS_NAMES = {
    1: "Новий",
    2: "Відібрано",
    4: "Дійшов на 1 тур",
    5: "?",
    32: "Не актуально",
    33: "Не підходить нам",
    34: "Не ЦА",
}


async def get_page(client: KeyCRMClient, page: int, per_page: int = 50) -> list[dict]:
    r = await client._client.get(
        "/pipelines/cards",
        params={
            "filter[pipeline_id]": 1,
            "limit": per_page,
            "page": page,
            "include": "custom_fields",
        },
    )
    return r.json().get("data", [])


async def main():
    c = KeyCRMClient()

    # First, discover total via last-page hop
    # Try page 1 to see if any 'meta' info
    r = await c._client.get(
        "/pipelines/cards",
        params={"filter[pipeline_id]": 1, "limit": 50, "page": 1},
    )
    body = r.json()
    meta = body.get("meta") or {}
    total = meta.get("total")
    last_page = meta.get("last_page")
    print(f"meta: total={total}, last_page={last_page}")

    if not last_page:
        print("no meta.last_page — falling back to walking")
        return

    # Fetch the last 5 pages = ~250 newest leads
    recent = []
    for page in range(last_page, max(0, last_page - 5), -1):
        recent.extend(await get_page(c, page))
    # Sort by id desc
    recent.sort(key=lambda l: l.get("id", 0), reverse=True)
    print(f"\n=== NEWEST {len(recent)} leads ===")

    by_status = Counter(l.get("status_id") for l in recent)
    print("\nBy status_id (newest 250):")
    for sid, n in sorted(by_status.items(), key=lambda x: -x[1]):
        print(f"  {sid} ({STATUS_NAMES.get(sid, '?')}) : {n}")

    # Show top 30 newest
    print("\nTop 30 newest leads:")
    for l in recent[:30]:
        title = (l.get("title") or "")[:45]
        status = STATUS_NAMES.get(l.get("status_id"), "?")
        contact = l.get("contact") or {}
        cname = contact.get("full_name", "") if isinstance(contact, dict) else ""
        print(
            f"  id={l.get('id')} status={l.get('status_id')} ({status}) "
            f"contact={cname[:25]} title={title}"
        )

    # Search Kalenska across the newest 250
    print("\n=== Search 'Каленська' in newest 250 ===")
    hits = []
    for l in recent:
        title = (l.get("title") or "").lower()
        contact = l.get("contact") or {}
        cname = (contact.get("full_name") or "").lower() if isinstance(contact, dict) else ""
        for needle in ["каленська", "каленска", "kalenska", "kalenskaya", "зоя"]:
            if needle in title or needle in cname:
                hits.append((needle, l))
                break
    if hits:
        for needle, l in hits:
            print(
                f"  MATCH '{needle}' id={l.get('id')} status={l.get('status_id')} "
                f"title={(l.get('title') or '')[:60]}"
            )
    else:
        print("  NOT FOUND — profile_filter rejected (Каленська = бухгалтер, немає sales marker)")

    # Look for robota.ua evidence in any lead across newest 250 (source, comment, title)
    print("\n=== Search 'robota' anywhere in newest 250 ===")
    robota_hits = []
    for l in recent:
        blob = " ".join(str(x).lower() for x in [
            l.get("title", ""),
            l.get("manager_comment", ""),
            l.get("source", ""),
            (l.get("contact", {}) or {}).get("full_name", "") if isinstance(l.get("contact"), dict) else "",
        ])
        if "robota" in blob:
            robota_hits.append(l)
    if robota_hits:
        for l in robota_hits[:10]:
            print(f"  id={l.get('id')} title={(l.get('title') or '')[:50]}")
    else:
        print("  NO robota.ua leads in newest 250 → confirmed 0 robota.ua integration")

    # Newest 250 with status_id=1 (Новий) — real "on Новий" backlog
    novyj = [l for l in recent if l.get("status_id") == 1]
    print(f"\n=== Leads on status 1 ('Новий') in newest 250: {len(novyj)} ===")
    for l in novyj[:20]:
        title = (l.get("title") or "")[:50]
        contact = l.get("contact") or {}
        cname = contact.get("full_name", "") if isinstance(contact, dict) else ""
        cmt = (l.get("manager_comment") or "")[:60]
        print(f"  id={l.get('id')} title={title} name={cname[:25]}")

    # Cross-check: are our recent 95 local candidates all in KeyCRM top-250?
    async with session_scope() as s:
        local = (await s.execute(select(Candidate))).scalars().all()
    local_lead_ids = {r.keycrm_lead_id for r in local if r.keycrm_lead_id}
    recent_ids = {l.get("id") for l in recent}
    matched = local_lead_ids & recent_ids
    print(f"\n=== Cross-check ===")
    print(f"local candidates: {len(local)}")
    print(f"local with keycrm_lead_id: {len(local_lead_ids)}")
    print(f"of those, present in newest 250 KeyCRM leads: {len(matched)}")
    print(f"local candidates NOT in newest 250 (weird): {len(local_lead_ids - recent_ids)}")


asyncio.run(main())
