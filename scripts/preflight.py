"""Pre-flight check: every link in the chain, verified rather than assumed."""
import asyncio, json, os, sys, urllib.request
sys.path.insert(0, "/app")
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, func
from src.common.db import session_scope
from src.common.models import Call, Candidate, CandidateStatus
from src.common.settings import get_settings
from src.common.keycrm import KeyCRMClient
from src.common.keycrm_fields import STAGE_MAP
from src.call.script_template import render_system_prompt

ok, warn, fail = [], [], []


def check(cond, good, bad, soft=False):
    (ok if cond else (warn if soft else fail)).append(good if cond else bad)


async def main():
    s = get_settings()
    tz = ZoneInfo(s.app_timezone)
    print("now:", datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z"))
    print()

    # 1. prompt renders with the orchestrator's real arguments
    try:
        p = render_system_prompt(
            candidate_name="Тест", candidate_phone="+380000000000",
            candidate_position="менеджер", source="workua",
            vacancy_title="Менеджер", vacancy_pitch="p", vacancy_requirements="r",
            vacancy_salary="30000", vacancy_location="Україна")
        check("${" not in p, "prompt renders", "prompt has unsubstituted vars")
        check("правобережна" in p, "geo filter present", "GEO FILTER LOST")
        check("Age window (INTERNAL" in p, "age window present", "age window lost")
        check("бі-ту-бі" in p, "B2B pronunciation", "B2B not spelled out")
        check("NEVER speak more than two sentences" in p, "two-turn pitch", "monologue rule lost")
        check("Courses, studies, training" in p, "strict experience rule", "experience rule lost")
        check("soft_exit" not in p and "transfer_to_manager" not in p,
              "no phantom functions", "PHANTOM FUNCTIONS BACK")
    except Exception as e:
        fail.append(f"prompt render FAILED: {e}")

    # 2. CRM stage map filled
    check(all(v for v in STAGE_MAP.values()), "CRM stages mapped", "STAGE_MAP has zeros")
    check(STAGE_MAP.get("closed") == 33, "rejected -> Не підходить нам", "closed stage wrong")

    # 3. queue ready
    async with session_scope() as sess:
        counts = dict((r[0].value if hasattr(r[0], "value") else str(r[0]), r[1]) for r in (
            await sess.execute(select(Candidate.status, func.count(Candidate.id))
                               .group_by(Candidate.status))).all())
        stuck = (await sess.execute(
            select(func.count(Candidate.id)).where(Candidate.status == CandidateStatus.CALLING)
        )).scalar()
        total_calls = (await sess.execute(select(func.count(Call.id)))).scalar()
    print("queue:", counts)
    check(counts.get("in_call_queue", 0) + counts.get("new_resume", 0) > 0,
          f"queue has work: {counts.get('in_call_queue',0)} waiting",
          "queue is EMPTY — nobody to call tomorrow", soft=True)
    check(stuck == 0, "no stuck candidates",
          f"{stuck} stuck in CALLING (auto-requeued at 09:00)", soft=True)

    # 4. pause state — read it exactly the way the services do
    from src.bot.admin import calls_paused as _paused
    check(_paused() is False, "calls NOT paused", "CALLS ARE PAUSED")

    # 5. userbot reachable
    try:
        r = json.load(urllib.request.urlopen(f"{s.tguserbot_url}/health", timeout=10))
        check(r.get("ok"), f"telegram bot online ({r.get('me')})", "telegram bot unhealthy")
        check(r.get("active"), "telegram outreach active", "telegram outreach paused", soft=True)
    except Exception as e:
        fail.append(f"telegram bot unreachable: {e}")

    # 6. KeyCRM reachable
    try:
        kc = KeyCRMClient()
        async with session_scope() as sess:
            lead = (await sess.execute(
                select(Candidate.keycrm_lead_id).where(Candidate.keycrm_lead_id.isnot(None)).limit(1)
            )).scalar()
        if lead:
            d = await kc.get_lead(int(lead))
            check(d.get("id") == int(lead), "KeyCRM reachable", "KeyCRM read failed")
    except Exception as e:
        fail.append(f"KeyCRM error: {e}")

    print()
    for x in ok:
        print("  OK   ", x)
    for x in warn:
        print("  WARN ", x)
    for x in fail:
        print("  FAIL ", x)
    print()
    print(f"passed {len(ok)} | warnings {len(warn)} | failures {len(fail)}")

asyncio.run(main())
