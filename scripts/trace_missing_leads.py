"""Where did a named applicant stop on the way to the CRM? Read-only.

The recruiter's report always comes as names: "four in the work.ua cabinet, three
cards; Сулім Олександр is missing". This walks each name through the same facts
the pollers act on and prints the branch it took:

  * work.ua — the newest pages of the responses feed, the cursor and the
    skipped-jobs ledger;
  * robota.ua — the newest pages of the applies list, the cursor (seen, pending,
    unmapped, date);
  * our database — the candidate row, its status and whether its card still
    exists in KeyCRM.

It opens no contacts, downloads no files and writes nothing. robota.ua costs a
couple of list requests and is skipped while the Cloudflare cooldown is on.

    docker compose cp ../scripts/trace_missing_leads.py scheduler:/app/trace_missing_leads.py
    docker compose exec -T -w /app scheduler python3 trace_missing_leads.py \
        "Сулім Олександр" "Катерина Шевченко" "Чудновська Інна"
"""
from __future__ import annotations

import argparse
import asyncio
import re
from typing import Any

from src.integrations.robotaua_api import parse_add_date

_APOSTROPHES = re.compile("[’ʼ`′‘]")


def _words(text: str | None) -> list[str]:
    return _APOSTROPHES.sub("'", (text or "").casefold()).split()


def name_matches(query: str, name: str | None) -> bool:
    """Every word of the query is in the name, in any order. Boards disagree on
    order ("Ім'я Прізвище" / "Прізвище Ім'я") and some add a patronymic."""
    have = _words(name)
    want = _words(query)
    return bool(have and want) and all(w in have for w in want)


def workua_verdict(resp: Any, *, allowed: set[int], cursor: dict) -> str:
    """Which branch of workua_sync.poll_responses this response took."""
    if not resp.phone:
        return "без телефону — картку не створити (workua.response_no_phone)"
    if allowed and resp.job_id not in allowed:
        if str(resp.job_id) in (cursor.get("skipped_jobs") or {}):
            return (
                f"оголошення {resp.job_id} не в реєстрі — чекає в skipped_jobs; "
                "додайте id у панелі, наступний опит поверне відгук сам"
            )
        return f"оголошення {resp.job_id} не в реєстрі, і в skipped_jobs його немає"
    last = int(cursor.get("responses_last_id") or 0)
    if resp.id > last:
        return f"ще не опрацьовано (курсор на {last})"
    return "передано в інтейк — що далі, див. рядок бази нижче"


def robotaua_verdict(apply: dict, *, allowed: set[int], cursor: dict) -> str:
    """Which branch of robotaua_sync.poll_responses this apply took."""
    apply_id = str(apply.get("id") or 0)
    kind = str(apply.get("resumeType") or "")
    has_phone = bool((apply.get("phone") or "").strip())
    if kind == "Interaction":
        return "Interaction — перегляд/рекомендація, не відгук; у CRM не йде (правило 04.09)"
    vacancy = apply.get("vacancyId")
    if allowed and vacancy not in allowed:
        if apply_id in (cursor.get("unmapped") or {}):
            return f"вакансія {vacancy} не в реєстрі — чекає в unmapped; додайте id у панелі"
        return f"вакансія {vacancy} не в реєстрі — відгук пропущено"
    if apply_id in (cursor.get("pending") or {}):
        why = "кандидат приховав телефон" if apply_id in (cursor.get("phones_hidden") or []) \
            else "немає телефону або ліміт CV"
        return f"у черзі pending ({why})"
    if int(apply_id) in {int(x) for x in cursor.get("seen_ids") or []}:
        if has_phone:
            return "опрацьовано з телефоном — що далі, див. рядок бази нижче"
        return (
            f"опрацьовано без телефону ({kind or '?'}) — для вакансії без платних "
            "відкриттів це robotaua.intake_only_no_phone"
        )
    added = parse_add_date(apply.get("addDate"))
    last = parse_add_date(cursor.get("last_add_date"))
    if last is None or (added and added > last):
        return "ще не опрацьовано"
    return "курсор пройшов повз, а в seen/pending немає — втрачено (ліміт CV або збій)"


def card_verdict(lead_id: int | None, pipeline: int | None, *, lookup_failed: bool = False) -> str:
    if not lead_id:
        return "картки немає — рядок у базі є, у CRM нічого"
    if lookup_failed:
        return f"картка {lead_id}: KeyCRM не відповів, стан невідомий"
    if pipeline is None:
        return f"картку {lead_id} видалено в CRM"
    return f"картка {lead_id} у воронці {pipeline}"


def _mask(phone: str | None) -> str:
    return f"…{phone[-4:]}" if phone else "—"


async def _trace_db(names: list[str]) -> None:
    from sqlalchemy import select

    from src.common.db import session_scope
    from src.common.keycrm import KeyCRMClient
    from src.common.models import Candidate

    print("\n=== наша база ===")
    kc = KeyCRMClient()
    try:
        for name in names:
            async with session_scope() as s:
                q = select(Candidate)
                for word in _words(name):
                    q = q.where(Candidate.full_name.ilike(f"%{word}%"))
                rows = (await s.execute(q.order_by(Candidate.id))).scalars().all()
            if not rows:
                print(f"  {name}: у базі немає — до інтейку не дійшов")
            for c in rows:
                pipeline, failed = None, False
                if c.keycrm_lead_id:
                    try:
                        pipeline = await kc.card_pipeline(int(c.keycrm_lead_id))
                    except Exception:  # noqa: BLE001
                        failed = True
                created = c.created_at.strftime("%d.%m %H:%M") if c.created_at else "?"
                print(
                    f"  {name}: #{c.id} {c.full_name} {_mask(c.phone_e164)} | {c.status} | "
                    f"{c.source} | {c.vacancy_key or '—'} | {created} | "
                    + card_verdict(c.keycrm_lead_id, pipeline, lookup_failed=failed)
                )
    finally:
        await kc.aclose()


async def _trace_workua(names: list[str], pages: int) -> None:
    from src.integrations import workua_sync
    from src.integrations.workua_api import WorkUaClient, parse_response

    print("\n=== work.ua ===")
    cursor = workua_sync._load_cursor()
    allowed = workua_sync._allowed_vacancy_ids()
    print(f"  курсор: {cursor.get('responses_last_id')}, відомі оголошення: {sorted(allowed)}")
    client = WorkUaClient()
    found: set[str] = set()
    try:
        before = None
        for _ in range(pages):
            page = await client.list_responses(
                limit=50, sort=0, before_id=before, from_types=["send", "phonecall"]
            )
            items = page.get("items") or []
            if not items:
                break
            for raw in items:
                resp = parse_response(raw)
                for name in names:
                    if name_matches(name, resp.fio):
                        found.add(name)
                        when = f"{resp.date:%d.%m %H:%M}" if resp.date else "?"
                        print(
                            f"  {name}: відгук {resp.id} {when} | оголошення {resp.job_id} | "
                            f"{resp.from_type}/{resp.type} | "
                            + workua_verdict(resp, allowed=allowed, cursor=cursor)
                        )
            before = min(int(r["id"]) for r in items)
    finally:
        await client.aclose()
    for name in names:
        if name not in found:
            print(f"  {name}: серед {pages * 50} найновіших відгуків немає")


async def _trace_robotaua(names: list[str], pages: int) -> None:
    from src.integrations import robotaua_sync
    from src.integrations.robotaua_api import RobotaUaClient, blocked_until

    print("\n=== robota.ua ===")
    cursor = robotaua_sync.load_cursor()
    allowed = robotaua_sync.allowed_vacancy_ids()
    print(f"  курсор: {cursor.get('last_add_date')}, відомі вакансії: {sorted(allowed)}")
    for bucket in ("pending", "unmapped"):
        for apply_id, entry in (cursor.get(bucket) or {}).items():
            for name in names:
                if name_matches(name, entry.get("name")):
                    print(
                        f"  {name}: у {bucket} — apply {apply_id}, вакансія {entry.get('vacancy_id')}, "
                        f"{entry.get('resume_type')}, з {entry.get('first_seen')}"
                    )
    until = blocked_until()
    if until:
        print(f"  Cloudflare cooldown до {until:%d.%m %H:%M} UTC — список відгуків не читаю")
        return
    client = RobotaUaClient()
    found: set[str] = set()
    for page in range(pages):
        applies = await client.list_applies(page=page, count=50)
        if not applies:
            break
        for apply in applies:
            for name in names:
                if name_matches(name, apply.get("name")):
                    found.add(name)
                    print(
                        f"  {name}: apply {apply.get('id')} {str(apply.get('addDate'))[:16]} | "
                        f"вакансія {apply.get('vacancyId')} | {apply.get('resumeType')} | "
                        + robotaua_verdict(apply, allowed=allowed, cursor=cursor)
                    )
        await asyncio.sleep(3)
    for name in names:
        if name not in found:
            print(f"  {name}: серед {pages * 50} найновіших відгуків немає")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("names", nargs="+", help='"Прізвище Ім\'я" — порядок слів неважливий')
    ap.add_argument("--workua-pages", type=int, default=4)
    ap.add_argument("--robotaua-pages", type=int, default=2)
    ap.add_argument("--skip-robotaua", action="store_true")
    args = ap.parse_args()

    steps = [_trace_workua(args.names, args.workua_pages)]
    if not args.skip_robotaua:
        steps.append(_trace_robotaua(args.names, args.robotaua_pages))
    steps.append(_trace_db(args.names))
    for step in steps:
        try:
            await step
        except Exception as e:  # noqa: BLE001 — one board down must not hide the others
            print(f"  ⚠️ {type(e).__name__}: {str(e)[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
