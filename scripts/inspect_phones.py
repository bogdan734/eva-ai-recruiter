import asyncio, json
from src.common.settings import get_settings
from src.integrations.workua_api import WorkUaClient, parse_response


PHONES = {"+380506400876", "+380673673820", "+380974837600"}


async def main():
    s = get_settings()
    c = WorkUaClient(email=s.workua_employer_email, password=s.workua_employer_password)
    try:
        r = await c.list_responses_for_vacancy(8249916, limit=100)
    finally:
        await c.aclose()
    items = r.get("items") or []
    print(f"vacancy 8249916 total items: {len(items)}")
    for it in items:
        try:
            p = parse_response(it)
        except Exception:
            continue
        if p.phone in PHONES:
            print(f"\n=== MATCH {p.phone} ({p.fio}) ===")
            print(f"  id={p.id} job_id={p.job_id} from_type={p.from_type}")
            print(f"  date={p.date}")
            print(f"  type={p.type} with_file={p.with_file}")
            print(f"  cover: {(p.cover or '')[:200]}")
            print(f"  text : {(p.text or '')[:200]}")
            print(f"  raw keys: {list(it.keys())}")
            # dump interesting fields
            for k in ("resume", "candidate", "from_type", "vacancy", "job"):
                if k in it:
                    print(f"  {k}: {json.dumps(it[k], ensure_ascii=False)[:200]}")


asyncio.run(main())
