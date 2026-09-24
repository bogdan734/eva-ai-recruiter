import asyncio
import os

import httpx

API_KEY = os.environ["VAPI_API_KEY"]

CALLS = {
    "01a032a4-3add-7770-9371-78baba532080": "call1_2026-08-24_inbound_11sec.wav",
    "01a06d0f-461e-7000-81f2-995a90b3d3e0": "call2_2026-09-04_outbound_36sec.wav",
}


async def main():
    async with httpx.AsyncClient(
        base_url="https://api.vapi.ai",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=60.0,
    ) as client:
        for cid, fname in CALLS.items():
            r = await client.get(f"/call/{cid}")
            c = r.json()
            artifact = c.get("artifact") or {}
            url = artifact.get("presignedMonoUrl") or c.get("recordingUrl")
            print(cid, "-> download url:", url)
            if not url:
                print("  NO URL FOUND, skipping")
                continue
            async with httpx.AsyncClient(timeout=60.0) as plain_client:
                rr = await plain_client.get(url)
            print("  download status:", rr.status_code, "bytes:", len(rr.content))
            out_path = f"/app/{fname}"
            with open(out_path, "wb") as f:
                f.write(rr.content)
            print("  saved to", out_path)


asyncio.run(main())
