"""Full KeyCRM + local DB reconciliation audit."""
import asyncio
from collections import Counter

from src.common.keycrm import KeyCRMClient
from src.common.db import session_scope
from src.common.models import Candidate
from sqlalchemy import select


async def fetch_all_leads(client: KeyCRMClient) -> list[dict]:
    all_leads: list[dict] = []
    page = 1
    while True:
        r = await client._client.get(
            "/pipelines/cards",
            params={
                "filter[pipeline_id]": 1,
                "limit": 50,
                "page": page,
                "sort": "-id",
                "include": "custom_fields",
            },
        )
        data = r.json().get("data", [])
        if not data:
            break
        all_leads.extend(data)
        if len(data) < 50:
            break
        page += 1
        if page > 40:  # 2000 lead safety cap
            break
    return all_leads


async def main():
    c = KeyCRMClient()
    leads = await fetch_all_leads(c)
    print(f"=== KeyCRM total leads in funnel 1: {len(leads)} ===\n")

    # By status
    by_status = Counter(l.get("status_id") for l in leads)
    print("By status_id:")
    STATUS_NAMES = {
        1: "Новий",
        2: "Відібрано",
        4: "Дійшов на 1 тур",
        32: "Не актуально",
        33: "Не підходить нам",
        34: "Не ЦА",
    }
    for sid, n in sorted(by_status.items(), key=lambda x: -x[1]):
        print(f"  {sid} ({STATUS_NAMES.get(sid, '?')}) : {n}")

    # By source
    by_source: Counter = Counter()
    for l in leads:
        src = l.get("source")
        if isinstance(src, dict):
            name = src.get("name") or f"id={src.get('id')}"
        elif isinstance(src, int):
            name = f"id={src}"
        else:
            name = "none"
        by_source[name] += 1
    print("\nBy source:")
    for name, n in by_source.most_common():
        print(f"  {name}: {n}")

    # Manager comment scan for source hints (Apix-Drive puts source in manager_comment)
    print("\nSource hints from manager_comment (top 15 substrings):")
    comment_hints: Counter = Counter()
    for l in leads:
        cmt = (l.get("manager_comment") or "").lower()
        for kw in ["work.ua", "workua", "workua_response", "robota", "robota.ua",
                  "apix", "apixdrive", "manual", "inbound_call"]:
            if kw in cmt:
                comment_hints[kw] += 1
    for kw, n in comment_hints.most_common(15):
        print(f"  '{kw}': {n}")

    # Search for Kalenska Zoya
    print("\n=== Search for 'Каленська' / 'Каленска' / 'Kalenska' ===")
    hits = []
    for l in leads:
        title = (l.get("title") or "").lower()
        contact = l.get("contact") or {}
        cname = (contact.get("full_name") or "").lower() if isinstance(contact, dict) else ""
        cmt = (l.get("manager_comment") or "").lower()
        for needle in ["каленська", "каленска", "kalenska", "kalenskaya"]:
            if needle in title or needle in cname or needle in cmt:
                hits.append(l)
                break
    if hits:
        for l in hits:
            print(
                f"  id={l.get('id')} status_id={l.get('status_id')} "
                f"title={l.get('title')[:60]}"
            )
    else:
        print("  NOT FOUND in KeyCRM → profile filter blocked her (expected — accountant)")

    # Last 10 leads
    print("\n=== Last 20 KeyCRM leads (recent activity) ===")
    for l in leads[:20]:
        src = l.get("source")
        src_name = (
            src.get("name")
            if isinstance(src, dict)
            else str(src) if src else "None"
        )
        stage_name = STATUS_NAMES.get(l.get("status_id"), "?")
        print(
            f"  id={l.get('id')} status={l.get('status_id')} ({stage_name}) "
            f"src={src_name} title={(l.get('title') or '')[:40]}"
        )

    # Local DB
    print("\n=== Local DB candidates ===")
    async with session_scope() as s:
        rows = (await s.execute(select(Candidate))).scalars().all()
        print(f"  total: {len(rows)}")
        db_by_src = Counter(r.source for r in rows)
        for src, n in db_by_src.most_common():
            print(f"    src={src}: {n}")
        db_by_status = Counter(r.status for r in rows)
        for st, n in db_by_status.most_common():
            print(f"    status={st}: {n}")
        # KeyCRM lead_id linkage
        no_lead = sum(1 for r in rows if not r.keycrm_lead_id)
        with_lead = sum(1 for r in rows if r.keycrm_lead_id)
        print(f"  no keycrm_lead_id: {no_lead}, with: {with_lead}")

    # Cross-reference: local ↔ keycrm
    print("\n=== Cross-check: local candidates present in KeyCRM ===")
    async with session_scope() as s:
        local = (await s.execute(select(Candidate))).scalars().all()
    keycrm_ids = {l.get("id") for l in leads}
    local_lead_ids = {r.keycrm_lead_id for r in local if r.keycrm_lead_id}
    orphan_local = local_lead_ids - keycrm_ids
    only_keycrm = keycrm_ids - local_lead_ids
    print(f"  local candidates with KeyCRM lead: {len(local_lead_ids)}")
    print(f"  matching in KeyCRM: {len(local_lead_ids & keycrm_ids)}")
    print(f"  orphan local (has lead_id but no matching KeyCRM lead): {len(orphan_local)}")
    print(f"  KeyCRM leads NOT linked to any local candidate: {len(only_keycrm)}")
    if only_keycrm:
        # Sample the top 5 for source check
        sample = [l for l in leads if l.get("id") in only_keycrm][:5]
        print("  sample of KeyCRM-only leads:")
        for l in sample:
            src = l.get("source")
            src_name = src.get("name") if isinstance(src, dict) else str(src) if src else "None"
            print(f"    id={l.get('id')} src={src_name} title={(l.get('title') or '')[:40]}")


asyncio.run(main())
