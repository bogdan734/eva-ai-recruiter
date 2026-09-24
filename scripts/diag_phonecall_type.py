"""Is work.ua's "перегляди телефону" the same thing as from_type=phonecall?

The recruiter cannot find a "подзвонив по вакансії" section in the cabinet, and
the only phone-shaped number on her screen is "6 переглядів телефону". Those
could be the same event counted twice, or two different events entirely. Guessing
is cheap and wrong; the responses feed knows.

Pulls phonecall rows only, and prints what the API actually returns for each --
which job, when, and what else is attached to the row.
"""
import asyncio
import json
from collections import Counter

from src.integrations.workua_api import WorkUaClient, parse_response


async def main() -> None:
    c = WorkUaClient()

    for label, types in (("ТІЛЬКИ phonecall", ["phonecall"]), ("ТІЛЬКИ send", ["send"])):
        try:
            raw = await c.list_responses(limit=200, sort=0, from_types=types)
        except Exception as e:
            print(label, "-> помилка:", type(e).__name__, e)
            continue
        items = raw.get("items") or raw.get("data") or []
        print(f"=== {label}: {len(items)} записів у вікні ===")
        rows = []
        for it in items:
            try:
                rows.append(parse_response(it))
            except Exception:
                pass
        by_job = Counter(str(r.job_id) for r in rows)
        for job, n in sorted(by_job.items()):
            print(f"   оголошення {job}: {n}")
        if types == ["phonecall"] and items:
            print("   --- сирий вигляд першого запису ---")
            print("  ", json.dumps(items[0], ensure_ascii=False)[:700])
            print("   --- поіменно ---")
            for r in sorted(rows, key=lambda x: x.date or __import__("datetime").datetime.min):
                d = r.date.strftime("%d.%m %H:%M") if r.date else "—"
                print(f"   {d} | job {r.job_id} | {(r.fio or '')[:26]:26} | "
                      f"тип CV: {r.type} | телефон: {'є' if r.phone else 'нема'}")
        print()

    await c.aclose()


asyncio.run(main())
