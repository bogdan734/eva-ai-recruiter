import asyncio

from src.common.settings import get_settings
from src.integrations.workua_api import WorkUaClient


async def main():
    s = get_settings()
    c = WorkUaClient(email=s.workua_employer_email, password=s.workua_employer_password)
    try:
        vacs = await c.list_my_vacancies(limit=100)
    finally:
        await c.aclose()
    for v in vacs.get("items", []):
        name = (v.get("name") or "")[:80]
        print(f"  id={v.get('id')} active={v.get('active')} name={name}")


asyncio.run(main())
