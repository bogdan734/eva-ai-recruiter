import asyncio
import json
import os

import httpx

API_KEY = os.environ["VAPI_API_KEY"]

CALL_IDS = [
    "01a0244d-ef14-755f-a181-8cb745b37609",  # 2026-08-21, assistant-said-end-call-phrase
    "01a032a4-3add-7770-9371-78baba532080",  # 2026-08-24, assistant-said-end-call-phrase
    "01a06d0f-461e-7000-81f2-995a90b3d3e0",  # 2026-09-04, customer-ended-call (my test call)
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
            print("createdAt:", c.get("createdAt"))
            print("endedReason:", c.get("endedReason"))
            print("cost:", c.get("cost"))
            print("customer:", c.get("customer"))
            print("metadata:", c.get("metadata"))
            # Look for anything recording/artifact/transcript related anywhere.
            def find_keys(d, prefix=""):
                found = []
                if isinstance(d, dict):
                    for k, v in d.items():
                        path = f"{prefix}.{k}" if prefix else k
                        lk = k.lower()
                        if any(kw in lk for kw in ("record", "audio", "artifact", "transcript", "summary", "url")):
                            found.append((path, v if not isinstance(v, (dict, list)) else type(v).__name__))
                        found.extend(find_keys(v, path))
                elif isinstance(d, list):
                    for i, item in enumerate(d[:2]):
                        found.extend(find_keys(item, f"{prefix}[{i}]"))
                return found

            hits = find_keys(c)
            print("relevant fields found:")
            for path, val in hits:
                print(f"  {path} = {val!r}"[:300])
            print()
            print("ALL TOP-LEVEL KEYS:", sorted(c.keys()))
            print()


asyncio.run(main())
