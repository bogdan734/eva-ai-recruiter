"""Bulk-push work.ua responses missing from CRM for a given set of job ids.

Fixed 2026-09-03 after Заблоцька Дарія (an ~1-month-old active candidate)
got a duplicate "Новий" card: the old dedup only checked our LOCAL
candidates.phone_e164 table. That misses anyone whose phone already exists
in KeyCRM but was never round-tripped into our local DB (manually entered by
a recruiter, imported, or created by a code path that predates local
tracking). KeyCRM buyers/contacts ARE phone-searchable (unlike cards), so
this now also checks find_buyer_by_phone against the *whole* CRM, across
every pipeline/status, before creating anything -- not just active "Новий"
leads. A phone that already has a KeyCRM buyer is routed to a third bucket
(`existing_buyer`) and is NOT auto-pushed; it needs a human to decide whether
it's a genuine repeat applicant (fine, push normally) or someone already
mid-relationship elsewhere (do not duplicate) -- the API cannot tell those
apart cheaply, so this script no longer guesses.

Fixed again same day, second bug: push_one() creates the KeyCRM card FIRST
and only inserts the local Candidate row SECOND. When that second step used
to fail (typically phone_e164's unique constraint -- two response records in
the same run, or a rerun, sharing a phone), the exception was caught and
silently counted as a success: no local row, no error line, nothing. That
card then became invisible to every later script that queries "which
candidates need X" via the local table -- which is exactly how 90 cards from
this same script sat unarchived and unnoticed until the recruiter found them
by hand. Local-write failures are now retried, then -- on a genuine
collision -- either attached to the existing free local row, or, if that
row is already claimed by a different card, appended to a durable on-disk
log (`state/orphan_keycrm_cards.jsonl`) and surfaced explicitly in the run's
own report. A card can no longer disappear from view just because a DB
write raced or collided.

    docker compose exec -T -w /app scheduler python3 bulk_push_workua_gap.py            # dry run
    docker compose exec -T -w /app scheduler python3 bulk_push_workua_gap.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.common import vacancies
from src.common.db import session_scope
from src.common.keycrm import DEFAULT_MANAGER_ID, KeyCRMClient, crm_source_id
from src.common.models import Candidate, CandidateStatus
from src.common.vacancy_link import vacancy_number_and_url
from src.integrations.workua_api import WorkUaClient
from src.match import profile_filter as pf

JOBS = [8346465, 8249916]
TODAY_NOTE = "03.09"

# Repo-root-relative state dir, same place workua_cursor.json etc live.
ORPHAN_LOG_PATH = Path(__file__).resolve().parent.parent / "state" / "orphan_keycrm_cards.jsonl"


async def fetch_all(client: WorkUaClient, jid: int) -> dict[int, dict]:
    all_items: dict[int, dict] = {}
    before_id = None
    for _ in range(50):
        params = {"limit": 50}
        if before_id is not None:
            params["before_id"] = before_id
        r = await client._client.get(f"/jobs/{jid}/responses/", params=params)
        if r.status_code != 200:
            break
        try:
            data = r.json()
        except Exception:
            break
        items = data.get("items") or []
        if not items:
            break
        new_count = 0
        for it in items:
            rid = int(it["id"])
            if rid not in all_items:
                all_items[rid] = it
                new_count += 1
        if new_count == 0:
            break
        min_id = min(int(it["id"]) for it in items)
        if before_id is not None and min_id >= before_id:
            break
        before_id = min_id
        if len(items) < 50:
            break
    return all_items


def norm_phone(p: str | None) -> str | None:
    if not p:
        return None
    digits = re.sub(r"\D", "", p)
    if digits.startswith("380") and len(digits) == 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+38" + digits
    if digits:
        return "+" + digits
    return None


def classify(jid: int, it: dict) -> pf.FilterResult:
    by = it.get("birth_date") or ""
    birth_year = int(by[:4]) if by[:4].isdigit() and by[:4] != "0000" else None
    resume_text = ((it.get("text") or "") + "\n\n" + (it.get("cover") or "")).strip() or None
    return pf.evaluate(
        full_name=it.get("fio"),
        region=None,
        desired_position=None,
        last_position=None,
        resume_text=resume_text,
        experience_text=resume_text,
        education_text=None,
        birth_year=birth_year,
        gender=None,
        country="UA",
    )


async def gather(keycrm: KeyCRMClient) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    wu_client = WorkUaClient()
    async with session_scope() as s:
        rows = (await s.execute(select(Candidate.phone_e164))).scalars().all()
    known_phones = {p for p in rows if p}

    no_phone: list[dict] = []
    existing_buyer: list[dict] = []
    group1: list[dict] = []
    group2: list[dict] = []
    seen_this_run: set[str] = set()

    try:
        for jid in JOBS:
            items = await fetch_all(wu_client, jid)
            for rid, it in items.items():
                ph = norm_phone(it.get("phone"))
                if not ph:
                    no_phone.append({"jid": jid, "rid": rid, "fio": it.get("fio")})
                    continue
                if ph in known_phones or ph in seen_this_run:
                    continue
                seen_this_run.add(ph)

                # Full-CRM phone check (not just local DB) -- catches anyone
                # KeyCRM already knows under any pipeline/status.
                try:
                    buyer_id = await keycrm.find_buyer_by_phone(ph)
                except Exception:
                    buyer_id = None  # fail open on the check itself; don't block the whole run
                await asyncio.sleep(0.15)
                if buyer_id:
                    existing_buyer.append({"jid": jid, "rid": rid, "it": it, "phone": ph, "buyer_id": buyer_id})
                    continue

                res = classify(jid, it)
                entry = {"jid": jid, "rid": rid, "it": it, "phone": ph, "reason": res.reason}
                (group1 if res.accepted else group2).append(entry)
    finally:
        await wu_client.aclose()

    return no_phone, existing_buyer, group1, group2


def resolve_collision(existing_lead_id: int | None) -> str:
    """Given the keycrm_lead_id already on the local row that collided on
    phone_e164, decide the recovery action.

    None (or falsy) means that row is free -- our new card can be attached
    to it directly, no data lost. A real id means that row already points at
    a DIFFERENT card, so attaching would silently orphan the one it already
    tracks; the new card has to go to the durable orphan log instead. Pulled
    out as a pure function so the decision can be tested without a live DB.
    """
    return "attach" if not existing_lead_id else "orphan"


def record_orphan_card(
    lead_id: int, full_name: str, phone: str, reason: str, *, log_path: Path = ORPHAN_LOG_PATH
) -> None:
    """Durable, append-only fallback for a KeyCRM card that could not be
    linked to a local Candidate row. A print()/log line alone was the old
    behaviour and is how 90 cards went untracked for hours -- this survives
    the process exiting and gives any later script (or a human) a fixed
    place to look for "created in CRM, not yet local" cards."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "lead_id": lead_id,
        "full_name": full_name,
        "phone": phone,
        "reason": reason,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def link_local_record(
    *,
    lead_id: int,
    full_name: str,
    phone: str,
    email: str | None,
    work_ua_url: str | None,
    resume_text: str | None,
    source: str,
    status_value: str,
    vacancy_key: str,
    log_path: Path = ORPHAN_LOG_PATH,
) -> tuple[str, str]:
    """Make sure a just-created KeyCRM card is always findable locally.

    Retries transient DB errors a few times; on a genuine phone_e164
    collision either attaches the card to the existing free row or -- if
    that row already belongs to a different card -- records it to the
    orphan log instead of dropping it. Returns ("linked" | "orphan", info).
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            async with session_scope() as s:
                s.add(
                    Candidate(
                        full_name=full_name,
                        phone_e164=phone,
                        email=email,
                        work_ua_url=work_ua_url,
                        resume_text=resume_text,
                        source=source,
                        status=status_value,
                        vacancy_key=vacancy_key,
                        keycrm_lead_id=lead_id,
                    )
                )
            return "linked", str(lead_id)
        except IntegrityError:
            try:
                async with session_scope() as s:
                    existing = (
                        await s.execute(select(Candidate).where(Candidate.phone_e164 == phone))
                    ).scalar_one_or_none()
                    if existing is not None and resolve_collision(existing.keycrm_lead_id) == "attach":
                        existing.keycrm_lead_id = lead_id
                        return "linked", f"{lead_id} (прив'язано до наявного запису {existing.id})"
            except Exception as e:  # noqa: BLE001 -- the recovery lookup itself failed; fall through to orphan log
                last_exc = e
                break
            last_exc = None
            break  # existing row already claimed by a different card -- orphan log below
        except Exception as e:  # noqa: BLE001 -- transient error, retry
            last_exc = e
            await asyncio.sleep(0.5 * (attempt + 1))

    reason = str(last_exc)[:200] if last_exc else "phone_e164 вже зайнятий іншою карткою"
    record_orphan_card(lead_id, full_name, phone, reason, log_path=log_path)
    return "orphan", f"{lead_id} (ORPHAN -- залоговано в {log_path.name}: {reason})"


async def push_one(client: KeyCRMClient, entry: dict, *, group2: bool) -> tuple[str, str]:
    """Returns (status, info) where status is "ok" (card created and
    linked locally), "orphan" (card created in KeyCRM but NOT linked --
    see state/orphan_keycrm_cards.jsonl), or "error" (KeyCRM creation
    itself failed, no card exists)."""
    jid = entry["jid"]
    it = entry["it"]
    phone = entry["phone"]
    full_name = (it.get("fio") or "Кандидат work.ua").strip()
    route = vacancies.for_workua(jid)
    source = f"workua_response_{it.get('from_type') or 'send'}"
    number, url = vacancy_number_and_url(source, jid, route)

    resume_url = None
    cand_id = it.get("candidate_id")
    if cand_id:
        resume_url = f"https://www.work.ua/employer/my/applicants/{cand_id}/?jobId={jid}"

    resume_text = ((it.get("text") or "") + (("\n\n" + it["cover"]) if it.get("cover") else "")).strip() or None

    if group2:
        comment = f"не пройшов автоматичний фільтр: {entry['reason']}. Підкочено {TODAY_NOTE} (розбір прогалини work.ua), потребує ручної перевірки менеджером."
    else:
        comment = f"пропущений раніше через технічний розрив в інтейку, профіль виглядає відповідним. Підкочено {TODAY_NOTE}."

    try:
        created = await client.create_lead(
            title=full_name,
            full_name=full_name,
            phone=phone,
            email=it.get("email"),
            vacancy_name=route.label,
            vacancy_number=number,
            vacancy_url=url,
            resume_text=resume_text,
            resume_url=resume_url,
            manager_comment=comment,
            pipeline_id=route.keycrm_pipeline_id,
            status_id=1,  # "Новий"
            source_id=crm_source_id(source),
            manager_id=DEFAULT_MANAGER_ID,
            save_buyer=False,  # 2026-09-03: recruiter reviews/saves manually
        )
        lead_id = int(created.get("id") or 0)
        if not lead_id:
            return "error", "KeyCRM не повернув id картки"
    except Exception as e:  # noqa: BLE001 -- one bad row must not stop the run
        return "error", str(e)[:200]

    status, info = await link_local_record(
        lead_id=lead_id,
        full_name=full_name,
        phone=phone,
        email=it.get("email"),
        work_ua_url=resume_url,
        resume_text=resume_text,
        source=source,
        status_value=(CandidateStatus.MANAGER_REVIEW.value if group2 else CandidateStatus.NEW_RESUME.value),
        vacancy_key=route.key,
    )
    return status, info


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    client = KeyCRMClient()
    no_phone, existing_buyer, group1, group2 = await gather(client)
    print(f"без телефону (пропущено, картку створити нічим): {len(no_phone)}")
    for x in no_phone:
        print(f"   job={x['jid']} rid={x['rid']} {x['fio']}")
    print(f"вже є buyer у KeyCRM (НЕ підкочуємо автоматично, на ручний розгляд): {len(existing_buyer)}")
    for x in existing_buyer:
        print(f"   {x['it'].get('fio')} ({x['phone']}) buyer_id={x['buyer_id']}")
    print(f"group1 (технічна прогалина, виглядає відповідним): {len(group1)}")
    print(f"group2 (відсіяні фільтром, manager_review): {len(group2)}")
    print(f"разом до підкочування: {len(group1) + len(group2)}")

    if not args.apply:
        print("\nDRY RUN. Нічого не створено. Далі: --apply")
        await client.aclose()
        return

    g1_ok = g1_orphan = g1_err = g2_ok = g2_orphan = g2_err = 0
    errors: list[str] = []
    orphans: list[str] = []
    try:
        for entry in group1:
            status, info = await push_one(client, entry, group2=False)
            if status == "ok":
                g1_ok += 1
            elif status == "orphan":
                g1_orphan += 1
                orphans.append(f"[g1] {entry['it'].get('fio')} ({entry['phone']}): {info}")
            else:
                g1_err += 1
                errors.append(f"[g1] {entry['it'].get('fio')} ({entry['phone']}): {info}")
            await asyncio.sleep(0.3)

        for entry in group2:
            status, info = await push_one(client, entry, group2=True)
            if status == "ok":
                g2_ok += 1
            elif status == "orphan":
                g2_orphan += 1
                orphans.append(f"[g2] {entry['it'].get('fio')} ({entry['phone']}): {info}")
            else:
                g2_err += 1
                errors.append(f"[g2] {entry['it'].get('fio')} ({entry['phone']}): {info}")
            await asyncio.sleep(0.3)
    finally:
        await client.aclose()

    print(f"\n=== РЕЗУЛЬТАТ ===")
    print(f"group1: створено і прив'язано {g1_ok}, orphan {g1_orphan}, помилок {g1_err}")
    print(f"group2: створено і прив'язано {g2_ok}, orphan {g2_orphan}, помилок {g2_err}")
    print(f"без телефону (не підкочено): {len(no_phone)}")
    print(f"пропущено як вже-є-buyer (на ручний розгляд): {len(existing_buyer)}")
    if orphans:
        print(f"\n⚠️  ORPHAN -- картка є в KeyCRM, локального запису НЕМАЄ. Залоговано в {ORPHAN_LOG_PATH}:")
        for o in orphans:
            print("  ", o)
    if errors:
        print("\nПомилки:")
        for e in errors:
            print("  ", e)


if __name__ == "__main__":
    asyncio.run(main())
