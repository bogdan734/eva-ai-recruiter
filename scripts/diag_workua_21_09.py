"""Why did the cabinet show 10 responses on 21.09 while 15 cards appeared?

Ask work.ua itself rather than reasoning about it. Three explanations are
testable from the API in one pass:

  * from_type -- we pull "send" AND "phonecall"; a recruiter counting the
    default list in the cabinet may be seeing only one of them;
  * job_id -- responses arrive across several postings, and a count taken on
    one posting will be short;
  * date vs cursor -- a response submitted on the 20th but read by our cursor
    on the 21st lands on our 21st and the cabinet's 20th.
"""
import asyncio
from collections import Counter

from src.integrations.workua_api import WorkUaClient, parse_response


async def main() -> None:
    c = WorkUaClient()
    try:
        raw = await c.list_responses(limit=200, sort=0, from_types=["send", "phonecall"])
    except Exception as e:
        print("list_responses failed:", type(e).__name__, e)
        return

    items = raw.get("items") or raw.get("data") or []
    print("отримано записів:", len(items))
    rows = []
    for it in items:
        try:
            rows.append(parse_response(it))
        except Exception:
            pass
    print("розібрано:", len(rows))
    if not rows:
        print("сирий приклад:", str(items[:1])[:400])
        return

    days = Counter()
    day_type = Counter()
    day_job = Counter()
    for r in rows:
        day = r.date.strftime("%d.%m") if r.date else "—"
        days[day] += 1
        day_type[(day, r.from_type or "?")] += 1
        day_job[(day, str(r.job_id))] += 1

    print()
    print("=== усього за днями ===")
    for d, n in sorted(days.items()):
        print(f"  {d}  {n}")

    print()
    print("=== день × тип відгуку ===")
    for (d, t), n in sorted(day_type.items()):
        print(f"  {d}  {t:<12} {n}")

    print()
    print("=== день × оголошення ===")
    for (d, j), n in sorted(day_job.items()):
        print(f"  {d}  оголошення {j:<12} {n}")

    print()
    print("=== 21.09 поіменно (id / час / оголошення / тип / ПІБ) ===")
    for r in sorted((x for x in rows if x.date and x.date.strftime("%d.%m") == "21.09"),
                    key=lambda x: x.date):
        print(f"  {r.id} | {r.date.strftime('%d.%m %H:%M')} | job {r.job_id} | "
              f"{r.from_type:<10} | {(r.fio or '')[:30]}")

    await c.aclose()


asyncio.run(main())
