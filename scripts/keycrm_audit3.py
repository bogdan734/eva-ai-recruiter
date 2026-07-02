"""KeyCRM audit v3 — Laravel-style pagination, default DESC order."""
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
    5: "?stage5",
    32: "Не актуально",
    33: "Не підходить нам",
    34: "Не ЦА",
}


async def main():
    c = KeyCRMClient()

    # Page 1 with limit 50 → default is DESC by id
    r = await c._client.get(
        "/pipelines/cards",
        params={
            "filter[pipeline_id]": 1,
            "limit": 50,
            "page": 1,
            "include": "custom_fields",
        },
    )
    body = r.json()
    total = body.get("total")
    last_page = body.get("last_page")
    per_page = body.get("per_page")
    print(f"=== KeyCRM funnel 1: total={total}, last_page={last_page}, per_page={per_page} ===\n")

    # Pull first 5 pages = 250 newest
    all_recent = list(body.get("data", []))
    for page in range(2, 6):
        r = await c._client.get(
            "/pipelines/cards",
            params={"filter[pipeline_id]": 1, "limit": 50, "page": page, "include": "custom_fields"},
        )
        all_recent.extend(r.json().get("data", []))

    print(f"pulled {len(all_recent)} NEWEST leads (page 1-5, IDs {all_recent[-1]['id']}..{all_recent[0]['id']})\n")

    by_status = Counter(l.get("status_id") for l in all_recent)
    print("Newest 250 — by status_id:")
    for sid, n in sorted(by_status.items(), key=lambda x: -x[1]):
        print(f"  {sid} ({STATUS_NAMES.get(sid, '?')}) : {n}")

    # Source distribution
    src_counter: Counter = Counter()
    for l in all_recent:
        src = l.get("source")
        if isinstance(src, dict):
            src_counter[src.get("name") or f"id={src.get('id')}"] += 1
        elif src:
            src_counter[f"raw={src}"] += 1
        else:
            src_counter["none"] += 1
    print("\nNewest 250 — by source:")
    for name, n in src_counter.most_common():
        print(f"  {name}: {n}")

    # manager_comment source hints
    print("\nmanager_comment substrings (in newest 250):")
    cmt_counter: Counter = Counter()
    for l in all_recent:
        cmt = (l.get("manager_comment") or "").lower()
        for kw in ["work.ua", "workua", "workua_response", "robota", "robota.ua",
                  "apix", "manual", "inbound_call", "джерело"]:
            if kw in cmt:
                cmt_counter[kw] += 1
    for kw, n in cmt_counter.most_common():
        print(f"  '{kw}': {n}")

    # Search for Kalenska + robota anywhere
    print("\n=== Search 'Каленська' / robota in newest 250 ===")
    for l in all_recent:
        blob = " ".join(str(x).lower() for x in [
            l.get("title", ""),
            l.get("manager_comment", "") or "",
            (l.get("contact", {}) or {}).get("full_name", "") if isinstance(l.get("contact"), dict) else "",
        ])
        if any(n in blob for n in ["каленська", "каленска", "kalenska", "зоя"]):
            print(f"  KALENSKA-like id={l.get('id')} title={(l.get('title') or '')[:60]}")
        if "robota" in blob:
            print(f"  ROBOTA.UA id={l.get('id')} title={(l.get('title') or '')[:60]}")

    # Newest on status Новий
    novyj = [l for l in all_recent if l.get("status_id") == 1]
    print(f"\n=== Leads on 'Новий' (status 1) in newest 250: {len(novyj)} ===")
    print("Sample first 15 title/id:")
    for l in novyj[:15]:
        title = (l.get("title") or "")[:60]
        contact = l.get("contact") or {}
        cname = contact.get("full_name", "") if isinstance(contact, dict) else ""
        cmt = (l.get("manager_comment") or "")[:80]
        print(f"  id={l.get('id')} title={title}")
        if cname:
            print(f"     contact={cname}")
        if cmt:
            print(f"     cmt={cmt}")

    # Cross-check local ↔ KeyCRM newest 250
    async with session_scope() as s:
        local = (await s.execute(select(Candidate))).scalars().all()
    local_lead_ids = {r.keycrm_lead_id for r in local if r.keycrm_lead_id}
    recent_ids = {l.get("id") for l in all_recent}
    matched = local_lead_ids & recent_ids
    print(f"\n=== Cross-check local ↔ KeyCRM ===")
    print(f"  local candidates total: {len(local)}")
    print(f"  local with keycrm_lead_id set: {len(local_lead_ids)}")
    print(f"  matched in newest 250 KeyCRM: {len(matched)}")
    # Which local NOT matched
    missing = local_lead_ids - recent_ids
    if missing:
        print(f"  local lead_ids NOT in newest 250 (older or wrong id): {sorted(missing)[:10]}")

    # Detail on unmatched: fetch by id
    if missing:
        print("\n  probing 1 missing lead:")
        mid = next(iter(missing))
        r = await c._client.get(f"/pipelines/cards/{mid}", params={"include": "custom_fields"})
        d = r.json().get("data") or r.json()
        print(f"    id={d.get('id')} status={d.get('status_id')} title={(d.get('title') or '')[:60]}")

    # KeyCRM leads that came AFTER our ingestion started (source detection)
    print("\n=== Source hint via title patterns for newest 30 ===")
    for l in all_recent[:30]:
        title = (l.get("title") or "")[:50]
        src_id = l.get("source_id")
        stage = STATUS_NAMES.get(l.get("status_id"), "?")
        contact = l.get("contact") or {}
        cname = contact.get("full_name", "") if isinstance(contact, dict) else ""
        print(f"  id={l.get('id')} src_id={src_id} status={stage[:15]:15s} title={title} name={cname[:20]}")


asyncio.run(main())
