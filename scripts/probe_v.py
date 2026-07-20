import asyncio
from src.common.settings import get_settings
from src.integrations.workua_api import WorkUaClient, parse_response


async def main():
    s = get_settings()
    c = WorkUaClient(email=s.workua_employer_email, password=s.workua_employer_password)
    try:
        r = await c.list_responses_for_vacancy(8249916, limit=100)
    finally:
        await c.aclose()
    items = r.get("items") or []
    print(f"vacancy 8249916 responses: {len(items)}")
    for it in items[:15]:
        try:
            p = parse_response(it)
            print(f"  id={p.id} job={p.job_id} fio={p.fio} phone={p.phone} type={p.from_type}")
        except Exception as e:
            print(f"  parse fail: {e}")


asyncio.run(main())
