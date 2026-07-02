import asyncio

from src.common.settings import get_settings
from src.integrations.workua_api import WorkUaClient, parse_response


async def main():
    s = get_settings()
    c = WorkUaClient(email=s.workua_employer_email, password=s.workua_employer_password)
    try:
        raw = await c.list_responses(limit=200)
    finally:
        await c.aclose()
    items = raw.get("items") or raw.get("data") or []
    print(f"pulled {len(items)} responses")
    kalen = []
    for it in items:
        try:
            p = parse_response(it)
        except Exception:
            continue
        name = (p.fio or "").lower()
        if any(n in name for n in ["каленськ", "каленск", "kalensk", "зоя"]):
            kalen.append((p, it))
    print(f"KALENSKA hits: {len(kalen)}")
    for p, raw_it in kalen:
        rez = raw_it.get("resume") or {}
        print(f"  id={p.id} fio={p.fio} phone={p.phone} job_id={p.job_id}")
        print(f"    resume.position={rez.get('position')}")
        print(f"    resume.speciality={rez.get('speciality')}")
    print("\nfirst 5 samples:")
    for it in items[:5]:
        p = parse_response(it)
        rez = it.get("resume") or {}
        pos = rez.get("position") or rez.get("speciality")
        print(f"  id={p.id} fio={p.fio} pos={pos}")


asyncio.run(main())
