"""Trace the mystery leads — search across all recent work.ua responses."""
import asyncio, json
from src.common.settings import get_settings
from src.integrations.workua_api import WorkUaClient, parse_response


PHONES = {"+380506400876", "+380673673820", "+380974837600"}


async def main():
    s = get_settings()
    c = WorkUaClient(email=s.workua_employer_email, password=s.workua_employer_password)
    try:
        # Fetch newest 200 from account-wide feed
        page = await c.list_responses(limit=200, sort=1, from_types=["send", "phonecall"])
    finally:
        await c.aclose()
    items = page.get("items") or []
    print(f"account-wide fetched: {len(items)}")
    for it in items:
        try:
            p = parse_response(it)
        except Exception:
            continue
        if p.phone in PHONES:
            print(f"\n=== MATCH {p.phone} ({p.fio}) ===")
            print(f"  id={p.id} job_id={p.job_id} from_type={p.from_type} date={p.date}")


asyncio.run(main())
