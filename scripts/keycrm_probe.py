import asyncio
from src.common.keycrm import KeyCRMClient


async def main():
    c = KeyCRMClient()
    r = await c._client.get(
        "/pipelines/cards",
        params={"filter[pipeline_id]": 1, "limit": 50, "page": 1, "sort": "-id"},
    )
    data = r.json().get("data", [])
    print(f"pulled {len(data)} recent leads (funnel 1)")
    sources = {}
    for lead in data:
        src = lead.get("source", {})
        src_name = src.get("name") if isinstance(src, dict) else str(src)
        sources[src_name] = sources.get(src_name, 0) + 1
    print("source counts:", sources)
    print()
    print("last 20 leads:")
    for lead in data[:20]:
        src = lead.get("source", {})
        src_name = src.get("name") if isinstance(src, dict) else str(src)
        title = (lead.get("title") or "")[:30]
        print(
            f"  id={lead.get('id')} status_id={lead.get('status_id')} "
            f"src={src_name} title={title}"
        )


asyncio.run(main())
