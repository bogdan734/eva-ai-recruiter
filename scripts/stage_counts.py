import asyncio
from src.common.keycrm import KeyCRMClient


async def main():
    c = KeyCRMClient()
    for st in [1, 32, 33, 34, 2, 4, 5]:
        r = await c._client.get(
            "/pipelines/cards",
            params={"filter[pipeline_id]": 1, "filter[status_id]": st, "limit": 1},
        )
        b = r.json()
        print(f"  status_id={st}: total={b.get('total')}")


asyncio.run(main())
