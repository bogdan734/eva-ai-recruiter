"""Why is a given robota.ua applicant not in the CRM?

The client keeps finding people in the robota.ua cabinet who never reached the
funnel, and each time the cause has been different: an unregistered vacancy, a
hidden phone, a screening rule shaped for another role. Guessing which one it is
costs a round of questions; this prints the whole decision path for a named
person in one pass.

    python scripts/diag_leads.py Арапина Антипенко "Мельник Світлана"
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from src.common import vacancies
from src.common.db import session_scope
from src.common.models import Candidate
from src.common.phone import normalize_phone
from src.integrations.robotaua_api import RobotaUaClient
from src.integrations.robotaua_sync import fit_score


async def main() -> None:
    wanted = [w.lower() for w in sys.argv[1:]]
    if not wanted:
        raise SystemExit("вкажіть прізвища: python scripts/diag_leads.py Арапина ...")

    registry = {}
    for key, vac in vacancies.all_vacancies().items():
        for vid in vac.robotaua_ids:
            registry[vid] = key

    async with session_scope() as s:
        rows = (await s.execute(select(Candidate.phone_e164, Candidate.full_name))).all()
    by_phone = {r[0]: r[1] for r in rows}
    names_in_db = {(r[1] or "").lower() for r in rows}

    client = RobotaUaClient()
    applies = []
    for page in (0, 1, 2):
        applies += await client.list_applies(page=page, count=50)
    print(f"переглянуто відгуків: {len(applies)}\n")

    for raw in applies:
        name = raw.get("name") or ""
        if not any(w in name.lower() for w in wanted):
            continue

        vid = raw.get("vacancyId")
        route = vacancies.for_robotaua(vid)
        phone = normalize_phone(raw.get("phone") or "") if raw.get("phone") else None

        print(f"── {name}")
        print(f"   вакансія {vid}: {registry.get(vid, '❌ НЕ В РЕЄСТРІ')}")
        print(f"   тип запису: {raw.get('resumeType')}   додано: {raw.get('addDate')}")
        print(f"   телефон у фіді: {phone or '— прихований'}")
        print(f"   контакт відкрито: {raw.get('isOpenContact')}")
        print(f"   spec: {str(raw.get('speciality'))[:70]!r}")

        if route is None:
            print("   ВЕРДИКТ: відгук відкидається — вакансії немає в реєстрі\n")
            continue

        print(f"   маршрут: {route.key} (воронка {route.keycrm_pipeline_id}, "
              f"платні контакти {route.open_paid_contacts})")
        print(f"   fit_score: {fit_score(raw, route)}")

        if phone and phone in by_phone:
            print(f"   ВЕРДИКТ: вже в базі як «{by_phone[phone]}» — дубль, це нормально\n")
        elif any(w in n for n in names_in_db for w in wanted):
            print("   ВЕРДИКТ: схоже ім'я вже в базі — перевірити вручну\n")
        elif phone:
            print("   ВЕРДИКТ: телефон є і вакансія відома — мав пройти, "
                  "дивитись лог інтейку\n")
        else:
            print("   ВЕРДИКТ: телефон прихований — потрібне відкриття контакту, "
                  "рішення за fit_score вище\n")

    await client._client.aclose() if hasattr(client, "_client") else None


if __name__ == "__main__":
    asyncio.run(main())
