import asyncio
from src.common.keycrm import KeyCRMClient


async def main():
    c = KeyCRMClient()
    for st in [1, 32, 33, 34, 2, 4, 5]:
        r = await c._client.get(
            "/pipelines/cards",
            params={
                "filter[pipeline_id]": 1,
                "filter[status_id]": st,
                "limit": 50,
            },
        )
        b = r.json()
        lp = b.get("last_page")
        pp = b.get("per_page")
        data_len = len(b.get("data") or [])
        # Fetch last page to get remainder
        remainder = 0
        if lp and lp > 1:
            r2 = await c._client.get(
                "/pipelines/cards",
                params={
                    "filter[pipeline_id]": 1,
                    "filter[status_id]": st,
                    "limit": 50,
                    "page": lp,
                },
            )
            remainder = len(r2.json().get("data") or [])
            total = (lp - 1) * pp + remainder
        else:
            total = data_len
        print(f"  status_id={st}: total≈{total} (last_page={lp})")


asyncio.run(main())
