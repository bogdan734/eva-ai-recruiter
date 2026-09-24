import asyncio
import json
import os

import httpx

API_KEY = os.environ["VAPI_API_KEY"]

CALL_IDS = [
    "01a0244d-ef14-755f-a181-8cb745b37609",
    "01a032a4-3add-7770-9371-78baba532080",
    "01a06d0f-461e-7000-81f2-995a90b3d3e0",
]


async def main():
    async with httpx.AsyncClient(
        base_url="https://api.vapi.ai",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=30.0,
    ) as client:
        for cid in CALL_IDS:
            r = await client.get(f"/call/{cid}")
            c = r.json()
            print("=" * 30, cid, "=" * 30)
            if "error" in c and "id" not in c:
                print("ERROR:", json.dumps(c, ensure_ascii=False))
                print()
                continue
            print("startedAt:", c.get("startedAt"), "endedAt:", c.get("endedAt"))
            print("FULL TRANSCRIPT:")
            print(c.get("transcript"))
            print()
            print("MESSAGES COUNT:", len(c.get("messages") or []))
            for m in (c.get("messages") or []):
                role = m.get("role")
                content = m.get("message") or m.get("content")
                print(f"  [{role}] {content}")
            print()
            print("SUMMARY:", c.get("summary"))
            print()


asyncio.run(main())
