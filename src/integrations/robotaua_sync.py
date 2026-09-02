"""robota.ua «Відгуки» → intake → CRM.

Mirrors `workua_sync.poll_responses()`: pull the employer cabinet's responses,
pre-filter them, and hand each one to the InboundRouter, which dedups by phone,
applies the geo filter and (in deferred mode) leaves CRM card creation to the
post-call orchestrator. The scheduler runs this every ROBOTAUA_POLL_MINUTES.

Two things are specific to robota.ua and worth knowing before touching this:

1. **Hidden phones.** Only CVs of type `Notepad` (or contacts already opened in
   the cabinet) expose a number; `Interaction` CVs — around 85% of the flow —
   keep it behind robota.ua's "open contacts" action, which draws on the paid
   package. The client authorised that budget on 2026-08-03, so the poller opens
   contacts itself — but ONLY for applies the funnel would actually accept
   (whitelisted oblast + sales/logistics position), capped per run and stopped
   the moment the quota hits zero. Everyone else is parked in `pending` and
   re-checked for free, so a recruiter opening a contact by hand also pulls that
   candidate in. Candidates who hid their number entirely answer
   `PhonesAreHidden`, cost nothing, and are never retried.

2. **Backfill.** The cabinet holds 7k+ historical responses. On the very first
   run (no cursor yet) we only look ROBOTAUA_BACKFILL_DAYS back — default 3 —
   so activating the poller can never dial years of old applicants.

Env:
    ROBOTAUA_EMPLOYER_EMAIL / _PASSWORD   credentials (required)
    ROBOTAUA_ALLOWED_VACANCY_IDS          csv of vacancy ids; empty = all
    ROBOTAUA_BACKFILL_DAYS                first-run lookback window (default 3)
    ROBOTAUA_MAX_PAGES                    pages of 50 per poll (default 4)
    ROBOTAUA_MAX_CV_FETCH                 CV fetches per poll (default 15)
    ROBOTAUA_PENDING_TTL_DAYS             drop un-opened applies after N days (30)
    ROBOTAUA_BLOCK_COOLDOWN_MIN           pause after a Cloudflare challenge (30)
    ROBOTAUA_AUTO_OPEN_CONTACTS           1 = spend contact openings on candidates
                                          the funnel would accept (default 1)
    ROBOTAUA_AUTO_OPEN_PER_RUN            cap per poll (default 3)
    ROBOTAUA_DRY_RUN                      1 = log what would be ingested, ingest nothing
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog

from src.api.inbound_router import InboundRouter, IngestPayload
from src.common import vacancies
from src.common.regions import is_region_allowed, normalize_region
from src.common.settings import get_settings
from src.integrations.base import JobBoardProvider, PollResult
from src.integrations.resume_file import phone_from_file
from src.integrations.robotaua_api import (
    RobotaUaBlockedError,
    RobotaUaClient,
    RobotaUaError,
    parse_add_date,
    parse_apply,
    state_dir,
    write_status,
)
from src.match.profile_filter import FilterResult
from src.match.profile_filter import evaluate as profile_evaluate

log = structlog.get_logger()

# Stand-in verdict for vacancies whose candidates are not screened by the sales
# portrait — the recruiter reads the card instead.
_ACCEPTED = FilterResult(accepted=True, reason="intake_only_vacancy")

SOURCE = "robotaua_response"
CURSOR_NAME = "robotaua_cursor.json"
SEEN_LIMIT = 3000
# Cloudflare starts challenging this VPS after roughly fifty requests in a
# burst, so the poll is built to stay tiny: four list calls carry the whole
# window, and a CV is fetched only for someone we can actually dial. Parked
# applies are re-checked against those same list rows for free; only the ones
# that fell out of the window cost a request, and only a few per run.
def _int_env(name: str, default: int) -> int:
    """Module-level env read. `_env_int` below is defined too late for these."""
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


# Tuned down 06.08.2026: the poll itself was the burst Cloudflare kept flagging —
# city dict + 4 list pages + up to 15 CV fetches + 5 pending probes ≈ 26 requests
# inside ~40s, every 10 minutes. That tripped a challenge MID-poll, which raises
# the backoff, and robota.ua responses were down all day (one успішний прогін in
# ten hours). Smaller footprint, wider spacing.
PENDING_PROBE_PER_RUN = _int_env("ROBOTAUA_PENDING_PROBE_PER_RUN", 2)
MAX_CV_FETCH_DEFAULT = _int_env("ROBOTAUA_MAX_CV_FETCH_DEFAULT", 6)
REQUEST_PAUSE_SEC = float(os.getenv("ROBOTAUA_REQUEST_PAUSE_SEC") or 3.0)


@dataclass
class RobotaUaPollStats:
    fetched: int = 0
    new: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    profile_rejected: int = 0
    no_phone: int = 0
    pending: int = 0
    recovered: int = 0
    # Phones recovered from an uploaded CV — free, no paid contact opening.
    attached_file_phones: int = 0
    contacts_opened: int = 0
    quota_left: int | None = None
    errors: int = 0
    last_add_date: str | None = None
    dry_run: bool = False
    samples: list[str] = field(default_factory=list)

    def as_log(self) -> dict[str, int | str | bool | None]:
        return {
            k: v for k, v in self.__dict__.items() if k != "samples"
        }


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        log.warning("robotaua.bad_int_env", var=name)
        return default


def _env_flag(name: str, default: str = "1") -> bool:
    return (os.getenv(name) or default).strip().lower() not in ("0", "false", "no", "off", "")


def _dry_run() -> bool:
    return (os.getenv("ROBOTAUA_DRY_RUN") or "").strip().lower() in ("1", "true", "yes", "on")


def allowed_vacancy_ids(*, calls_only: bool = False) -> set[int]:
    """robota.ua vacancy ids we pull Відгуки for.

    Base is the vacancy registry; ROBOTAUA_ALLOWED_VACANCY_IDS is added on top.
    `calls_only=True` narrows this to the vacancies Єва actually works — the
    chat poller uses it so she never writes to an intake-only applicant.
    """
    raw = (os.getenv("ROBOTAUA_ALLOWED_VACANCY_IDS") or "").strip()
    out: set[int] = set(vacancies.robotaua_ids(calls_only=calls_only))
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            vid = int(tok)
        except ValueError:
            log.warning("robotaua.bad_vacancy_id_in_env", token=tok)
            continue
        # An env entry must never smuggle an intake-only vacancy into Єва's
        # working set — that is the whole point of the calls_only view.
        route = vacancies.for_robotaua(vid)
        if calls_only and route is not None and not route.calls_enabled:
            continue
        out.add(vid)
    return out


def _cursor_path():
    return state_dir() / CURSOR_NAME


def load_cursor() -> dict:
    try:
        data = json.loads(_cursor_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _start_cooldown(cursor: dict) -> None:
    """Park the poller after a Cloudflare challenge and persist that decision.

    Doubling per consecutive block: a 30-minute pause proved too short on
    2026-08-03 (the flag outlived it), and each probe into a live block only
    refreshes it. Backing off to hours costs nothing — responses sit in the
    cabinet until we read them.
    """
    base = _env_int("ROBOTAUA_BLOCK_COOLDOWN_MIN", 30)
    cap = _env_int("ROBOTAUA_BLOCK_COOLDOWN_MAX_MIN", 240)
    strikes = int(cursor.get("block_strikes") or 0) + 1
    minutes = min(base * (2 ** (strikes - 1)), cap)
    until = datetime.utcnow() + timedelta(minutes=minutes)
    cursor["blocked_until"] = until.isoformat(timespec="seconds")
    cursor["block_strikes"] = strikes
    save_cursor(cursor)
    log.warning(
        "robotaua.cooldown_started",
        minutes=minutes,
        strikes=strikes,
        until=cursor["blocked_until"],
    )


def save_cursor(cursor: dict) -> None:
    try:
        path = _cursor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cursor, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.error("robotaua.cursor_save_failed", error=str(e))


async def _ingest_one(
    router: InboundRouter,
    fields: dict,
    stats: RobotaUaPollStats,
    *,
    dry_run: bool,
) -> bool:
    """Profile-filter one parsed apply and push it into the funnel.

    Returns True when the candidate reached the intake (new or duplicate) — i.e.
    when it should stop being tracked as pending.
    """
    route = vacancies.for_robotaua(fields.get("vacancy_id")) or vacancies.DEFAULT

    # The portrait below is the sales one. An intake-only vacancy has its own
    # requirements and a human reads the card, so it goes straight through.
    profile = _ACCEPTED if not route.screen_enabled else profile_evaluate(
        full_name=fields["full_name"],
        region=fields.get("region_raw"),
        desired_position=fields.get("desired_position"),
        resume_text=fields.get("resume_text"),
        experience_text=fields.get("resume_text"),
        birth_year=fields.get("birth_year"),
    )
    if not profile.accepted:
        stats.profile_rejected += 1
        log.info(
            "robotaua.profile_rejected",
            apply=fields["apply_id"],
            name=fields["full_name"],
            reason=profile.reason,
        )
        return True

    if dry_run:
        stats.accepted += 1
        stats.samples.append(
            f"{fields['full_name']} | {fields.get('city') or '?'} | "
            f"{fields.get('desired_position') or '?'} | vac {fields.get('vacancy_id')}"
        )
        log.info("robotaua.dry_run_would_ingest", apply=fields["apply_id"], name=fields["full_name"])
        return True

    result = await router.ingest(
        IngestPayload(
            full_name=fields["full_name"],
            phone_raw=fields["phone_raw"],
            email=fields.get("email"),
            region_raw=fields.get("region_raw"),
            desired_position=fields.get("desired_position"),
            experience_years=fields.get("experience_years"),
            work_ua_url=fields.get("resume_url"),
            resume_text=fields.get("resume_text") or None,
            source=SOURCE,
            vacancy_id=vacancies.LOCAL_FK,  # local FK; robota.ua id lives in the log
            vacancy_key=route.key,
            board_vacancy_id=fields.get("vacancy_id"),
        )
    )
    if not result.accepted:
        stats.rejected += 1
        log.info(
            "robotaua.ingest_rejected",
            apply=fields["apply_id"],
            reason=result.reason,
            vacancy=fields.get("vacancy_id"),
        )
    elif result.duplicate:
        stats.duplicates += 1
    else:
        stats.accepted += 1
        log.info(
            "robotaua.ingested",
            apply=fields["apply_id"],
            candidate=result.candidate_id,
            name=fields["full_name"],
            region=fields.get("region_raw"),
            vacancy=fields.get("vacancy_id"),
        )
    return True


ROLE_MARKERS = ("логіст", "продаж", "менеджер", "sales", "logistic", "експедит", "закупів")

# Score for a vacancy whose role nobody has described. Sits above the threshold
# on purpose: "we cannot judge" must not read as "reject".
NEUTRAL_FIT = 50
MIN_FIT_DEFAULT = 50


# robota.ua record kinds that mean "this person applied", as opposed to
# `Interaction`, which only means they looked or were recommended.
APPLIED_TYPES = frozenset({"AttachedFile", "Notepad"})


def fit_score(apply: dict, route) -> int:
    """0–100: how well this record fits the vacancy, judged without paying.

    Openings are a fixed prepaid pool, so the choice of who to spend one on has
    to be made from what robota.ua gives away for free. That rules out the CV
    body and the phone; what is left is the desired speciality, the work
    history, robota.ua's own match flag and how complete the CV is. All four
    survive on an `Interaction` record, which is most of the traffic.

    A vacancy with no `role_markers` scores neutral rather than zero. Nobody has
    described that role yet, and a silent zero would quietly stop it collecting
    anyone — the failure mode this project keeps relearning.

    The record's kind matters more than any of that. `Interaction` is a view or
    a recommendation; `AttachedFile` and `Notepad` are a person deliberately
    answering the posting. Treating them alike produced the inversion the
    recruiter reported on 02.09 — someone who only looked scored 70 and got a
    card, someone who applied with a CV scored 15 and never reached the CRM.
    """
    markers = tuple(getattr(route, "role_markers", ()) or ())
    if not markers:
        return NEUTRAL_FIT

    def _hits(text: str) -> bool:
        low = (text or "").lower()
        return any(m in low for m in markers)

    score = 0
    # Answering the posting is the strongest thing robota.ua tells us for free.
    # It is worth roughly what a matching speciality is worth, so a real
    # application with a blank profile still clears the bar while an application
    # for an unrelated role does not.
    if str(apply.get("resumeType") or "") in APPLIED_TYPES:
        score += 50

    # Wanting the job is worth more than having done it: the speciality is what
    # the person is looking for now, the history is where they have been.
    spec = (apply.get("speciality") or "").strip()
    if _hits(spec):
        score += 55
    elif any(_hits(e.get("position") or "") for e in (apply.get("experiences") or [])):
        score += 30
    elif spec:
        # A speciality that names something else is evidence, not absence: this
        # person is looking for a different job. An empty one is merely unknown
        # and must not be punished — most `AttachedFile` records carry none, and
        # penalising silence is how real applicants got filtered out.
        score -= 20

    if apply.get("isMatchVacancy"):
        score += 15

    # Completeness only ever breaks a tie. A fully filled CV for the wrong job is
    # still the wrong job, so this alone can never reach the threshold.
    try:
        score += int(round(min(100, max(0, int(apply.get("summaryPercentage") or 0))) * 0.2))
    except (TypeError, ValueError):
        pass

    return max(0, min(100, score))


def worth_opening(apply: dict, region: str | None) -> bool:
    """Is this parked apply worth one of the account's paid contact openings?

    The per-vacancy switch is the veto: `open_paid_contacts` off means never,
    whatever else is true.

    Past that, the question splits by what happens to the person next.

    For a vacancy Eva dials, an opening only pays off if the intake would keep
    them, so the sales guards stand: whitelisted oblast, sales/logistics title.
    A first pass over the 49 parked applies found 44 sitting in oblasts the geo
    filter blocks — every one of those would have been an opening burnt to reveal
    a number nobody was allowed to call.

    For an intake-only vacancy neither guard applies. Both are shaped for the
    sales portrait, and such a vacancy has its own geo and its own requirements
    with a human reading the card. Worse, robota.ua sends most records as
    `Interaction`, which carries no speciality and no phone — so the title test
    refuses nearly all of them and the vacancy produces almost no cards at all.
    That is the state the client wrote in about: applicants plainly visible in
    the robota.ua cabinet and absent from the CRM.

    Openings are prepaid and expire on a date whether or not they are spent, so
    for a vacancy a recruiter works by hand the useful question is only whether
    the vacancy wants them opened.
    """
    route = vacancies.for_robotaua(apply.get("vacancyId"))
    if route is not None and not route.open_paid_contacts:
        return False
    if route is not None and not route.screen_enabled:
        # Nobody dials these, so the sales guards below say nothing useful. What
        # does matter is not burning a limited pool on people the recruiter
        # would not look at twice.
        return fit_score(apply, route) >= _env_int("ROBOTAUA_MIN_FIT", MIN_FIT_DEFAULT)
    if not region:
        return False
    s = get_settings()
    if not is_region_allowed(normalize_region(region), s.regions_allowed, s.regions_blocked):
        return False
    spec = (apply.get("speciality") or "").lower()
    return any(marker in spec for marker in ROLE_MARKERS)


def _pending_as_apply(apply_id: str, entry: dict) -> dict:
    """Rebuild an apply-shaped dict from a parked entry.

    Parked entries store the same facts under snake_case keys, so the intake
    helpers — which expect robota.ua's own camelCase payload — can be reused on
    the backlog instead of duplicated. `filePath` may be missing on entries
    parked before the CV reader existed; `download_attachment` derives the URL
    from the apply id in that case.
    """
    return {
        "id": int(apply_id),
        "name": entry.get("name"),
        "vacancyId": entry.get("vacancy_id"),
        "cityId": entry.get("city_id"),
        "speciality": entry.get("speciality"),
        "resumeType": entry.get("resume_type"),
        "fileName": entry.get("file_name"),
        "filePath": entry.get("file_path"),
    }


async def _try_attached_file(
    client: RobotaUaClient,
    router: InboundRouter,
    apply: dict,
    cities: dict,
    stats: RobotaUaPollStats,
    *,
    dry: bool,
) -> bool:
    """Read the phone out of an uploaded CV, for applies that have no number.

    `AttachedFile` applies carry resumeId=0 and an empty phone: the candidate
    attached a document instead of filling robota.ua's resume form. The number is
    inside that file and nowhere else, so before this these people were parked
    forever — and opening their contacts costs quota that buys nothing, because
    robota.ua has no contact record to reveal.

    Returns True when the candidate reached the intake.
    """
    if str(apply.get("resumeType")) != "AttachedFile":
        return False
    if not _env_flag("ROBOTAUA_ATTACHED_FILE_ENABLED", "1"):
        return False

    apply_id = int(apply.get("id") or 0)
    file_url = (apply.get("filePath") or "").strip() or None
    file_name = (apply.get("fileName") or "").strip()

    if dry:
        stats.samples.append(f"WOULD READ CV: {apply.get('name')} | {file_name}")
        return False

    data = await client.download_attachment(apply_id, url=file_url)
    await asyncio.sleep(REQUEST_PAUSE_SEC)
    if not data:
        return False

    phone, text = phone_from_file(data, file_name)
    if not phone:
        log.info(
            "robotaua.attached_file_no_phone",
            apply=apply_id, file=file_name, chars=len(text or ""),
        )
        return False

    stats.attached_file_phones += 1
    log.info("robotaua.attached_file_phone", apply=apply_id, file=file_name)

    # Feed it through the same parser as everyone else, with the number and the
    # CV text we just recovered patched in.
    enriched = dict(apply)
    enriched["phone"] = phone
    fields = parse_apply(enriched, cities=cities)
    if text and not fields.get("resume_text"):
        fields["resume_text"] = text[:8000]
    return await _ingest_one(router, fields, stats, dry_run=False)


def row_phone(apply: dict) -> str:
    """Phone as the list payload shows it — empty when contacts are not open."""
    phone = (apply.get("phone") or "").strip()
    if phone:
        return phone
    contacts = apply.get("contacts") or {}
    phones = contacts.get("phones") or [] if isinstance(contacts, dict) else []
    return (phones[0].get("value") or "").strip() if phones else ""


async def _fields_for(client: RobotaUaClient, apply: dict, cities: dict) -> dict:
    """Parse an apply, enriching it with the full CV.

    Called only for applicants we can actually reach — the CV adds education,
    full work history and the resume text the CRM card and Єва's script use.
    """
    resume: dict | None = None
    resume_id = int(apply.get("resumeId") or 0)
    if resume_id:
        try:
            resume = await client.get_resume(resume_id)
        except RobotaUaError as e:
            log.warning("robotaua.resume_fetch_failed", resume=resume_id, error=str(e))
        await asyncio.sleep(REQUEST_PAUSE_SEC)
    return parse_apply(apply, cities=cities, resume=resume)


async def _try_auto_open(
    client: RobotaUaClient,
    router: InboundRouter,
    apply: dict,
    cities: dict,
    cursor: dict,
    stats: RobotaUaPollStats,
    *,
    dry: bool,
) -> bool:
    """Spend one contact opening on a parked apply that the funnel would take.

    Client authorised the account's 25 openings on 2026-08-03 for exactly this.
    Guards: only whitelisted regions with a sales/logistics position, a per-run
    cap, and a hard stop once robota.ua reports no openings left. Candidates who
    hid their number (`PhonesAreHidden`) are recorded so we never retry them.
    """
    if not _env_flag("ROBOTAUA_AUTO_OPEN_CONTACTS", "1"):
        return False
    resume_id = int(apply.get("resumeId") or 0)
    if not resume_id:
        return False
    apply_id = str(apply.get("id") or 0)
    if apply_id in set(cursor.get("phones_hidden") or []):
        return False

    city_id = apply.get("cityId")
    region = (cities.get(int(city_id)) or {}).get("region") if city_id else None
    if not worth_opening(apply, region):
        return False

    per_run = _env_int("ROBOTAUA_AUTO_OPEN_PER_RUN", 3)
    if stats.contacts_opened >= per_run:
        return False

    # One quota read per poll, and only once we actually have someone to open.
    if stats.quota_left is None:
        try:
            quota = await client.open_contacts_count()
            stats.quota_left = int((quota or {}).get("availableContacts") or 0)
        except RobotaUaError as e:
            log.warning("robotaua.quota_check_failed", error=str(e))
            stats.quota_left = 0
    if stats.quota_left <= 0:
        return False

    if dry:
        stats.samples.append(f"WOULD OPEN: {apply.get('name')} | {region}")
        return False

    outcome = await client.open_contact(resume_id)
    await asyncio.sleep(REQUEST_PAUSE_SEC)
    if outcome == "hidden":
        hidden = set(cursor.get("phones_hidden") or [])
        hidden.add(apply_id)
        cursor["phones_hidden"] = sorted(hidden)
        log.info("robotaua.phone_hidden_by_candidate", apply=apply_id, name=apply.get("name"))
        return False

    stats.contacts_opened += 1
    stats.quota_left -= 1
    opened = set(cursor.get("contacts_opened") or [])
    opened.add(apply_id)
    cursor["contacts_opened"] = sorted(opened)
    log.info(
        "robotaua.contact_opened",
        apply=apply_id,
        name=apply.get("name"),
        region=region,
        quota_left=stats.quota_left,
    )

    resume = await client.get_resume(resume_id)
    await asyncio.sleep(REQUEST_PAUSE_SEC)
    fields = parse_apply(apply, cities=cities, resume=resume)
    if not fields["phone_raw"]:
        return False
    await _ingest_one(router, fields, stats, dry_run=dry)
    return True


async def poll_responses(
    *,
    client: RobotaUaClient | None = None,
    router: InboundRouter | None = None,
    dry_run: bool | None = None,
) -> RobotaUaPollStats:
    """Pull new responses, ingest the reachable ones, park the rest."""
    dry = _dry_run() if dry_run is None else dry_run
    stats = RobotaUaPollStats(dry_run=dry)
    client = client or RobotaUaClient()
    router = router or InboundRouter()

    cursor = load_cursor()
    blocked_until = parse_add_date(cursor.get("blocked_until"))
    if blocked_until and datetime.utcnow() < blocked_until:
        # Cloudflare flagged the IP; polling through the block only keeps the
        # flag alive, so sit out until the cooldown expires.
        log.info("robotaua.cooldown", until=cursor.get("blocked_until"))
        stats.pending = len(cursor.get("pending") or {})
        # Still refresh the snapshot so /status shows the queue and the pause
        # instead of going blank while we sit out a Cloudflare block.
        write_status(
            pending=stats.pending,
            contacts_opened_total=len(cursor.get("contacts_opened") or []),
            phones_hidden_total=len(cursor.get("phones_hidden") or []),
            responses_blocked_until=cursor.get("blocked_until"),
        )
        return stats
    seen: set[int] = {int(x) for x in cursor.get("seen_ids") or []}
    pending: dict[str, dict] = dict(cursor.get("pending") or {})
    allowed = allowed_vacancy_ids()
    # Applies to postings nobody has mapped. Counted per run and reported once,
    # never dropped in silence: a republished vacancy arrives here as a live id
    # that matches nothing, and on work.ua exactly that carried 74% of the flow
    # into a bare `continue` for weeks.
    unknown_vacancies: Counter = Counter()
    backfill_days = _env_int("ROBOTAUA_BACKFILL_DAYS", 3)
    max_pages = _env_int("ROBOTAUA_MAX_PAGES", 4)

    last_seen_dt = parse_add_date(cursor.get("last_add_date"))
    if last_seen_dt is None:
        last_seen_dt = datetime.utcnow() - timedelta(days=backfill_days)
        log.info("robotaua.first_run", backfill_days=backfill_days, cutoff=last_seen_dt.isoformat())

    try:
        cities = await client.city_map()
    except RobotaUaError as e:
        log.warning("robotaua.city_dict_failed", error=str(e))
        cities = {}

    # ---- 1. one window of responses (4 calls, everything else is free) ----
    fresh: list[dict] = []
    window: dict[str, dict] = {}
    newest_dt = last_seen_dt
    try:
        for page in range(max_pages):
            applies = await client.list_applies(page=page, count=50)
            if not applies:
                break
            stats.fetched += len(applies)
            reached_old = False
            for apply in applies:
                apply_id = str(apply.get("id") or 0)
                window[apply_id] = apply
                added = parse_add_date(apply.get("addDate"))
                if added and added > newest_dt:
                    newest_dt = added
                if added and added <= last_seen_dt:
                    reached_old = True
                    continue
                if int(apply_id) in seen:
                    continue
                if allowed and apply.get("vacancyId") not in allowed:
                    unknown_vacancies[apply.get("vacancyId")] += 1
                    continue
                fresh.append(apply)
            if reached_old:
                break
            await asyncio.sleep(REQUEST_PAUSE_SEC)
    except RobotaUaBlockedError as e:
        stats.errors += 1
        log.warning("robotaua.blocked", error=str(e))
        if not dry:
            _start_cooldown(cursor)
        return stats
    except RobotaUaError as e:
        stats.errors += 1
        log.error("robotaua.list_failed", error=str(e))
        return stats

    if unknown_vacancies:
        # WARNING, not info: this is the signal that a posting was republished
        # under a new id and its applicants are going nowhere. Map it in the
        # panel (Параметри вакансії → вакансія → Збір і обдзвін).
        log.warning(
            "robotaua.unknown_vacancies",
            vacancies=dict(unknown_vacancies),
            skipped=sum(unknown_vacancies.values()),
        )

    max_cv = _env_int("ROBOTAUA_MAX_CV_FETCH", MAX_CV_FETCH_DEFAULT)
    cv_budget = max_cv
    blocked = False

    async def _handle_reachable(apply: dict) -> bool:
        """CV fetch + intake for an apply whose contacts are open."""
        nonlocal cv_budget, blocked
        if cv_budget <= 0:
            log.info("robotaua.cv_budget_spent", apply=apply.get("id"), budget=max_cv)
            return False
        cv_budget -= 1
        fields = await _fields_for(client, apply, cities)
        return await _ingest_one(router, fields, stats, dry_run=dry)

    stats.new = len(fresh)
    for apply in fresh:
        apply_id = int(apply.get("id") or 0)
        try:
            if row_phone(apply):
                if not await _handle_reachable(apply):
                    continue  # budget spent — retry on the next poll
            elif await _try_attached_file(client, router, apply, cities, stats, dry=dry):
                pass  # phone read out of the uploaded CV — free, no quota spent
            elif await _try_auto_open(client, router, apply, cities, cursor, stats, dry=dry):
                pass  # opened + ingested in one go
            else:
                # No CV fetch here on purpose: robota.ua hides the number for
                # `Interaction` CVs until contacts are opened in the cabinet, and
                # the CV endpoint would not reveal it either.
                stats.no_phone += 1
                _route = vacancies.for_robotaua(apply.get("vacancyId"))
                if _route is not None and not _route.open_paid_contacts:
                    # Never going to be opened, so parking it would only inflate
                    # the contact-opening queue the bot reports. The response is
                    # still visible to the recruiter in the robota.ua cabinet.
                    seen.add(apply_id)
                    log.info(
                        "robotaua.intake_only_no_phone",
                        apply=apply_id,
                        vacancy=apply.get("vacancyId"),
                        name=apply.get("name"),
                    )
                    continue
                pending[str(apply_id)] = {
                    "resume_id": int(apply.get("resumeId") or 0),
                    "name": (apply.get("name") or "").strip(),
                    "vacancy_id": apply.get("vacancyId"),
                    "resume_type": apply.get("resumeType"),
                    # Kept so a later maintenance pass can rank the backlog for
                    # contact opening without re-fetching the applies feed.
                    "city_id": apply.get("cityId"),
                    "speciality": apply.get("speciality"),
                    # `AttachedFile` applies carry the CV as an uploaded file and
                    # have resumeId=0 — there is no resume record to fetch, which
                    # is why /resume/{id} never found one. The phone lives inside
                    # that file. Capture the references now so working out the
                    # download URL later costs no extra requests: robota.ua bans
                    # this IP for hours when we probe it in bursts.
                    "file_name": apply.get("fileName"),
                    "file_path": apply.get("filePath"),
                    "resume_file": apply.get("resumeFile"),
                    "first_seen": datetime.utcnow().isoformat(timespec="seconds"),
                }
                log.info(
                    "robotaua.contacts_closed",
                    apply=apply_id,
                    name=apply.get("name"),
                    resume_type=apply.get("resumeType"),
                )
            seen.add(apply_id)
        except RobotaUaBlockedError as e:
            blocked = True
            log.warning("robotaua.blocked_mid_poll", apply=apply_id, error=str(e))
            break
        except Exception as e:  # noqa: BLE001 — one bad apply must not kill the poll
            stats.errors += 1
            seen.add(apply_id)
            log.warning("robotaua.apply_failed", apply=apply_id, error=str(e))

    # ---- 2. parked applies: has anyone opened the contacts since? ---------
    ttl_days = _env_int("ROBOTAUA_PENDING_TTL_DAYS", 30)
    stale_before = datetime.utcnow() - timedelta(days=ttl_days)
    probes_left = PENDING_PROBE_PER_RUN
    # Order matters more here than it looks. Only PENDING_PROBE_PER_RUN entries
    # get a request per poll, so a sort that depends solely on standing facts
    # picks the same few every single run and the rest of the backlog is never
    # looked at. That is exactly what happened 03–07.08: 103 polls, ~96 probes,
    # all of them spent re-checking the same two applies whose phones stayed
    # hidden, `recovered=0` every time while the queue grew 101 → 110.
    # `last_probe` rotates it: an entry just probed sorts to the back, so the
    # pointer walks the backlog instead of stalling on its head. The request
    # budget is untouched — still two probes per poll, and Cloudflare sees no
    # more traffic than before; they are simply two *different* applies.
    #
    # `last_probe` has to be the FIRST key, ahead of the paid-opening priority.
    # The other way round the same starvation just moves up a level: applies
    # whose contacts we opened would cycle among themselves forever and the
    # ~93 others would still never be looked at. Never-probed entries carry no
    # stamp, so they sort ahead of everyone — the whole backlog gets one probe
    # before anyone gets a second, and the paid openings only win among equals.
    opened_here = set(cursor.get("contacts_opened") or [])
    # Candidates who hid the number themselves: robota.ua already told us the
    # opening would reveal nothing, so never pay for them a second time.
    hidden_ids = set(cursor.get("phones_hidden") or [])
    opens_left = _env_int("ROBOTAUA_BACKLOG_OPEN_PER_RUN", 1)
    for apply_id in sorted(
        pending,
        key=lambda k: (
            pending[k].get("last_probe") or "",
            # Ours first among the never-probed: we spent quota on that phone,
            # the rest are just hoping a recruiter opens the contact.
            k not in opened_here,
            pending[k].get("first_seen") or "",
        ),
    ):
        if blocked:
            break
        entry = pending[apply_id]
        first_seen = parse_add_date(entry.get("first_seen"))
        if first_seen and first_seen < stale_before:
            pending.pop(apply_id, None)
            log.info("robotaua.pending_expired", apply=apply_id, name=entry.get("name"))
            continue

        row = window.get(apply_id)
        if row is None:
            # Fell out of the fetched window — costs a request, so only a few
            # of the oldest get probed per poll.
            if probes_left <= 0:
                continue
            resume_id = int(entry.get("resume_id") or 0)

            # An apply we have already probed once and whose phone is still
            # hidden will not come back on its own — nobody is going to open
            # that contact for us. Re-probing it every pass is what this loop
            # did for four days. If the funnel would actually take the person,
            # spend one of the account's authorised openings and be done.
            #
            # Budget: an opening costs two requests (open-contact, then the
            # resume) where a probe costs one, so it consumes the whole poll's
            # probe allowance. robota.ua therefore sees the same number of
            # requests per poll it saw before — the number the Cloudflare
            # backoff was tuned against. One opening per poll, quota permitting,
            # is ~4/hour: the 25 the client authorised are the binding limit
            # here, not the clock.
            if (
                opens_left > 0
                and resume_id
                and probes_left >= PENDING_PROBE_PER_RUN
                and entry.get("last_probe")  # never pay before checking for free
                and apply_id not in hidden_ids
                # Already bought once and still parked: the opening did not
                # produce a number, so buying it again buys the same nothing.
                # Three of the nine that pass the gate are in this state.
                and apply_id not in opened_here
            ):
                parked = _pending_as_apply(apply_id, entry)
                city_id = entry.get("city_id")
                region = (cities.get(int(city_id)) or {}).get("region") if city_id else None
                if worth_opening(parked, region):
                    opens_left -= 1
                    probes_left -= PENDING_PROBE_PER_RUN
                    entry["last_probe"] = datetime.utcnow().isoformat(timespec="seconds")
                    try:
                        if await _try_auto_open(
                            client, router, parked, cities, cursor, stats, dry=dry
                        ):
                            stats.recovered += 1
                            pending.pop(apply_id, None)
                    except RobotaUaBlockedError as e:
                        blocked = True
                        log.warning("robotaua.blocked_on_pending", apply=apply_id, error=str(e))
                        break
                    continue

            probes_left -= 1
            # Stamped before the request, not after: a probe that ends in a block
            # or an error has still cost us the slot, and leaving the stamp off
            # would hand this same entry the next poll's budget as well.
            entry["last_probe"] = datetime.utcnow().isoformat(timespec="seconds")
            if not resume_id:
                # `AttachedFile`: resumeId is 0 by design and /resume/{id} has
                # nothing to return, so the number only exists inside the
                # uploaded document. These used to dead-end here and sat in the
                # queue until the 30-day TTL dropped them. Reading the file costs
                # the one request a resume probe would have cost, and the entries
                # parked before the reader existed never captured `filePath` —
                # `download_attachment` derives it from the apply id instead.
                try:
                    if await _try_attached_file(
                        client, router, _pending_as_apply(apply_id, entry), cities, stats, dry=dry
                    ):
                        stats.recovered += 1
                        pending.pop(apply_id, None)
                except RobotaUaBlockedError as e:
                    blocked = True
                    log.warning("robotaua.blocked_on_pending", apply=apply_id, error=str(e))
                    break
                continue
            try:
                resume = await client.get_resume(resume_id)
            except RobotaUaBlockedError as e:
                blocked = True
                log.warning("robotaua.blocked_on_pending", apply=apply_id, error=str(e))
                break
            except RobotaUaError as e:
                log.warning("robotaua.pending_recheck_failed", apply=apply_id, error=str(e))
                continue
            await asyncio.sleep(REQUEST_PAUSE_SEC)
            if not (resume.get("phone") or "").strip():
                # A bare `continue` here is why a stalled queue stayed invisible
                # for four days: the only counter that moved was `pending`, and
                # it moves for other reasons too. One line per spent probe makes
                # `recovered=0` legible — you can see *who* was checked.
                log.info(
                    "robotaua.pending_probe_no_phone",
                    apply=apply_id,
                    name=entry.get("name"),
                    first_seen=entry.get("first_seen"),
                )
                continue
            row = {
                "id": int(apply_id),
                "resumeId": resume_id,
                "name": entry.get("name"),
                "vacancyId": entry.get("vacancy_id"),
                "resumeType": entry.get("resume_type"),
            }
            fields = parse_apply(row, cities=cities, resume=resume)
            stats.recovered += 1
            log.info("robotaua.contacts_opened", apply=apply_id, name=fields["full_name"])
            if await _ingest_one(router, fields, stats, dry_run=dry):
                pending.pop(apply_id, None)
            continue

        if not row_phone(row):
            continue
        stats.recovered += 1
        log.info("robotaua.contacts_opened", apply=apply_id, name=row.get("name"))
        try:
            if await _handle_reachable(row):
                pending.pop(apply_id, None)
        except RobotaUaBlockedError as e:
            blocked = True
            log.warning("robotaua.blocked_on_pending", apply=apply_id, error=str(e))
            break

    stats.pending = len(pending)
    # A poll cut short by Cloudflare must not move the date cursor past applies
    # it never looked at — `seen_ids` already keeps the processed ones out.
    stats.last_add_date = (
        cursor.get("last_add_date") if blocked else newest_dt.isoformat(timespec="seconds")
    )
    if blocked:
        stats.errors += 1

    if not dry:
        cursor.update(
            {
                "last_add_date": stats.last_add_date or newest_dt.isoformat(timespec="seconds"),
                "seen_ids": sorted(seen)[-SEEN_LIMIT:],
                "pending": pending,
                "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
            }
        )
        if blocked:
            _start_cooldown(cursor)  # also saves
        else:
            cursor.pop("blocked_until", None)
            cursor.pop("block_strikes", None)  # clean run resets the backoff
            save_cursor(cursor)

    # Keep the contact quota fresh for /status without spending a request every
    # poll: robota.ua only changes it when we open a contact.
    if not blocked and stats.quota_left is None:
        from src.integrations.robotaua_api import read_status

        prev = read_status()
        checked = parse_add_date(prev.get("quota_checked_at"))
        stale = checked is None or (datetime.utcnow() - checked) > timedelta(
            hours=_env_int("ROBOTAUA_QUOTA_REFRESH_HOURS", 6)
        )
        if stale:
            try:
                quota = await client.open_contacts_count()
                stats.quota_left = int((quota or {}).get("availableContacts") or 0)
                write_status(quota_checked_at=datetime.utcnow().isoformat(timespec="seconds"))
            except RobotaUaError as e:
                log.info("robotaua.quota_refresh_skipped", error=str(e)[:80])

    # Snapshot for the admin bot — /status must not hit robota.ua itself.
    write_status(
        pending=stats.pending,
        quota_left=stats.quota_left,
        contacts_opened_total=len(cursor.get("contacts_opened") or []),
        phones_hidden_total=len(cursor.get("phones_hidden") or []),
        responses_last_ok=(
            None if blocked else datetime.utcnow().isoformat(timespec="seconds")
        ),
        responses_blocked_until=cursor.get("blocked_until"),
    )
    log.info("robotaua.poll.done", **stats.as_log())
    return stats


class RobotaUaProvider(JobBoardProvider):
    """Registry adapter — the scheduler calls `poll_responses()` directly, this
    keeps robota.ua visible to the generic provider tooling (health checks)."""

    name = "robotaua"
    required_env = ("ROBOTAUA_EMPLOYER_EMAIL", "ROBOTAUA_EMPLOYER_PASSWORD")

    def __init__(
        self,
        client: RobotaUaClient | None = None,
        router: InboundRouter | None = None,
    ) -> None:
        self._client = client
        self._router = router

    async def poll_responses(self) -> PollResult:
        stats = await poll_responses(client=self._client, router=self._router)
        return PollResult(
            provider=self.name,
            fetched=stats.fetched,
            accepted=stats.accepted,
            duplicates=stats.duplicates,
            rejected=stats.rejected + stats.profile_rejected + stats.no_phone,
            errors=stats.errors,
            last_cursor=stats.last_add_date,
        )

    async def health_check(self) -> bool:
        try:
            client = self._client or RobotaUaClient()
        except Exception:
            return False
        return await client.health_check()
