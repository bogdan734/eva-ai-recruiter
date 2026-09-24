import asyncio
import json
import os

import httpx

API_KEY = os.environ["VAPI_API_KEY"]


async def main():
    async with httpx.AsyncClient(
        base_url="https://api.vapi.ai",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=30.0,
    ) as client:
        r = await client.get("/call", params={"limit": 1000})
        calls = r.json()
        print("status:", r.status_code)
        print(f"total calls fetched: {len(calls) if isinstance(calls, list) else '?'}")
        if not isinstance(calls, list):
            print(json.dumps(calls, indent=2, ensure_ascii=False))
            return

        calls.sort(key=lambda c: c.get("createdAt") or "")
        print("oldest:", calls[0].get("createdAt"))
        print("newest:", calls[-1].get("createdAt"))

        from collections import Counter
        cnt = Counter(c.get("endedReason") for c in calls)
        print()
        print("endedReason counts:")
        for k, v in cnt.most_common(20):
            print(v, k)


asyncio.run(main())
