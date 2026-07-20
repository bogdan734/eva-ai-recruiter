import asyncio
from collections import Counter
from src.common.settings import get_settings
from src.integrations.workua_api import WorkUaClient, parse_response


async def main():
    s = get_settings()
    c = WorkUaClient(email=s.workua_employer_email, password=s.workua_employer_password)
    try:
        page = await c.list_responses(limit=50, sort=1, from_types=["send", "phonecall"])
    finally:
        await c.aclose()
    items = page.get("items") or []
    print(f"pulled {len(items)}")
    jc: Counter = Counter()
    for it in items:
        p = parse_response(it)
        jc[p.job_id] += 1
    for job, n in jc.most_common():
        print(f"  job_id={job}: {n}")


asyncio.run(main())
