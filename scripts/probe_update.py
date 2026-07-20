import asyncio
from src.common.keycrm import KeyCRMClient


async def main():
    c = KeyCRMClient()
    # Take a Новий lead to test
    r = await c._client.get(
        "/pipelines/cards",
        params={"filter[pipeline_id]": 1, "filter[status_id]": 1, "limit": 1},
    )
    data = r.json().get("data") or []
    if not data:
        print("no leads on Новий left")
        return
    lead = data[0]
    lid = lead.get("id")
    print(f"probing lead id={lid} title={(lead.get('title') or '')[:40]}")

    # Try PUT
    for payload_desc, payload in [
        ("status_id_int", {"status_id": 32}),
        ("status_id_str", {"status_id": "32"}),
        ("nested", {"status": {"id": 32}}),
    ]:
        try:
            r2 = await c._client.put(f"/pipelines/cards/{lid}", json=payload)
            print(f"  {payload_desc}: HTTP {r2.status_code}: {r2.text[:200]}")
        except Exception as e:
            print(f"  {payload_desc}: EXC {type(e).__name__}: {e}")

    # Try move_stage which uses funnel_id in URL
    try:
        r3 = await c.move_stage(1, lid, 32)
        print(f"  move_stage: OK {r3}")
    except Exception as e:
        print(f"  move_stage: EXC {e}")


asyncio.run(main())
