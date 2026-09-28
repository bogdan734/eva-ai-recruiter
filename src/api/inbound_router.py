"""Inbound lead router.

Two modes controlled by DEFER_KEYCRM_UNTIL_QUALIFIED env (default: on):

  - deferred (default): only writes a local Candidate. KeyCRM lead is
    created later by the orchestrator's post-call `_finalize_call` after
    Єва's screening produces a decision. This keeps the CRM clean —
    only candidates who actually spoke with Єва land in the funnel.
    Sourced candidates only: a response (is_response) is never called, so
    it always gets its card at ingest.

  - eager (legacy): creates KeyCRM lead immediately on inbound. Kept for
    fallback if a client wants CRM to mirror raw work.ua activity.

Pipeline:
1. Normalize phone to E.164
2. Region pre-filter (whitelist + blacklist)
3. Dedup: check local DB and (in eager mode) KeyCRM by phone
4. Insert local Candidate
5. If eager mode — create KeyCRM lead and store lead_id on candidate
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select

from src.common import vacancies
from src.common.crm import CRMClient, get_crm
from src.common.db import session_scope
from src.common.keycrm import (
    DEFAULT_MANAGER_ID,
    FUNNEL_ID,
    STATUS_NEW,
    crm_source_id,
)
from src.common.vacancy_link import vacancy_number_and_url
from src.common.models import Candidate, CandidateStatus
from src.common.phone import normalize_phone
from src.common.regions import is_region_allowed, normalize_region
from src.common.settings import get_settings
from src.match.name_origin import is_slavic_name


def _defer_keycrm() -> bool:
    """When True, InboundRouter skips KeyCRM POST at ingest — the lead is
    created later by the orchestrator after the qualifying call."""
    raw = (os.getenv("DEFER_KEYCRM_UNTIL_QUALIFIED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")

log = structlog.get_logger()


@dataclass
class IngestPayload:
    full_name: str
    phone_raw: str
    email: str | None = None
    region_raw: str | None = None
    desired_position: str | None = None
    experience_years: int | None = None
    languages: list[str] | None = None
    work_ua_url: str | None = None
    workua_response_id: str | None = None
    resume_text: str | None = None
    source: str = "manual"
    match_score: float | None = None
    vacancy_id: int | None = None
    vacancy_name: str = "Менеджер з продажу"
    # Which vacancy of `src.common.vacancies` the person applied to. Decides the
    # KeyCRM funnel, whether the screening filters run and whether Єва calls.
    vacancy_key: str = vacancies.DEFAULT.key
    # The posting's own id on the board it arrived from — work.ua job_id or
    # robota.ua vacancyId. A vacancy holds several of these (a republication is
    # a new number for the same job), so the card can only point at the right
    # one if the id travels with the applicant.
    board_vacancy_id: int | None = None
    # When the applicant actually responded on the board (work.ua response
    # date / robota.ua addDate) -- NOT when we happened to poll it in. Without
    # this, every card looks equally 'new' regardless of whether the person
    # applied today or two months ago, and the recruiter cannot tell a hot
    # lead from a stale one. Reported by Svitlana 2026-09-03.
    response_date: datetime | None = None
    # 2026-09-04 policy change: True for anyone who applied/wrote to us
    # themselves (work.ua/robota.ua response, robota.ua chat) as opposed to
    # someone Єва is actively sourcing (workua_scraper, workua_search). A
    # response candidate always gets a card and never enters the call
    # queue -- recruiters triage responses by hand. Sourced candidates keep
    # the old behaviour: the portrait filter decides whether they even get a
    # card, because the only reason they exist in our system is for Єва to
    # call them.
    is_response: bool = False
    # A stand-in for phone_e164 when the board hides the number (robota.ua until a
    # paid opening), e.g. "rua<apply id>". The client (28.09) wants every applicant
    # in CRM, phone or not: one row per such application and a card with no phone.
    no_phone_key: str | None = None


@dataclass
class IngestResult:
    accepted: bool
    reason: str = ""
    candidate_id: int | None = None
    keycrm_lead_id: int | None = None
    duplicate: bool = False


def screening_applies(route, payload: IngestPayload) -> bool:
    """Should the region/name portrait gate run for this ingest?

    2026-09-04 policy change: screening now decides only who Єва calls, never
    who reaches the CRM. A self-applied candidate (payload.is_response) skips
    it entirely regardless of the vacancy; a sourced candidate still needs
    route.screen_enabled, exactly as before.
    """
    return bool(route.screen_enabled) and not payload.is_response


def intake_status(route, payload: IngestPayload) -> CandidateStatus:
    """Which status a freshly-ingested candidate starts at.

    MANAGER_REVIEW keeps a candidate out of the dialer -- the dispatcher only
    picks NEW_RESUME / IN_CALL_QUEUE, and the CRM stage sweep ignores this
    status too. 2026-09-04: a self-applied candidate is ALWAYS parked here
    regardless of calls_enabled -- Єва only dials people being actively
    sourced, never someone who already applied/wrote in himself.
    """
    if payload.is_response:
        return CandidateStatus.MANAGER_REVIEW
    return CandidateStatus.NEW_RESUME if route.calls_enabled else CandidateStatus.MANAGER_REVIEW


def _format_manager_comment(payload: IngestPayload, region: str | None) -> str:
    """Pack AI metadata into manager_comment (KeyCRM has no other free-form fields)."""
    bits: list[str] = []
    # First and most visible -- KeyCRM has no native response-date field
    # (checked 2026-09-03: none of the 12 lead custom fields is date-typed,
    # and POST /custom-fields is not supported -- 405, field creation is
    # UI-only). Until someone creates a real one, this is how a recruiter
    # tells a same-day applicant from a two-month-old one.
    if payload.response_date:
        bits.append(f"📅 дата відгуку: {payload.response_date.strftime('%d.%m.%Y %H:%M')}")
    if region:
        bits.append(region)
    if payload.experience_years:
        bits.append(f"досвід {payload.experience_years}р")
    if payload.languages:
        bits.append("мови: " + ", ".join(payload.languages))
    if payload.match_score is not None:
        bits.append(f"AI match {int(payload.match_score * 100)}/100")
    if payload.source and payload.source != "manual":
        bits.append(f"джерело: {payload.source}")
    # LD_1004 is «Посилання на вакансію» and now holds the posting, so the CV
    # link moves here rather than being dropped — KeyCRM renders it clickable.
    if payload.work_ua_url:
        bits.append(f"резюме: {payload.work_ua_url}")
    bits.append(f"стара_дата: {datetime.utcnow().isoformat(timespec='seconds')}")
    return " | ".join(bits)


# Width of `candidates.source`. Kept in sync with migration 0007 — the column
# used to be VARCHAR(32), which a candidate who applied through two channels
# already overflowed, killing the whole ingest transaction.
SOURCE_MAX_LEN = 128


def merge_sources(existing: str | None, new: str | None, *, limit: int = SOURCE_MAX_LEN) -> str:
    """Union of the channels a candidate reached us through, oldest tag first.

    Compares whole tags, not substrings: the old `new not in existing` test read
    a tag that happened to be a prefix of another as already recorded. When the
    result would not fit, the newest tag is dropped whole — a tag cut in half is
    worse than a tag missing, because the next merge would treat the fragment as
    a channel of its own.
    """
    tokens: list[str] = [t for t in (existing or "").split(",") if t]
    for tag in (new or "").split(","):
        if tag and tag not in tokens:
            tokens.append(tag)
    while tokens and len(",".join(tokens)) > limit:
        tokens.pop()
    return ",".join(tokens)


# Sources where a repeat is a NEW event rather than a duplicate to fold into
# the card we already have. A board response is the same person answering the
# same posting again -- one card, annotated. A Google-Form application is a
# fresh document the person filled in today, and the recruiter works each one
# on its own card at «Новий» (her request, 23.09.2026).
ALWAYS_NEW_CARD_SOURCES = frozenset({"googleform"})


class InboundRouter:
    def __init__(self, keycrm: CRMClient | None = None) -> None:
        self._keycrm = keycrm or get_crm()
        self._settings = get_settings()

    async def _note_repeat_response(
        self, existing: Candidate, payload: "IngestPayload", route
    ) -> None:
        """Surface a repeat response on a card we already have, instead of the
        local-duplicate return path swallowing it in silence (see the call
        site's comment). Reactivation only fires from a terminal give-up
        status -- CLOSED/UNREACHABLE -- because those are exactly the states
        where a recruiter would otherwise never see the person again; an
        actively-worked card is left exactly where it is, just annotated.
        """
        if not existing.keycrm_lead_id:
            log.info(
                "ingest.repeat_response_no_card",
                candidate_id=existing.id,
                status=existing.status,
            )
            return
        when = (payload.response_date or datetime.utcnow()).strftime("%d.%m.%Y %H:%M")
        note = f"🔁 повторний відгук {when} на «{route.label}» (джерело: {payload.source})"
        try:
            await self._keycrm.append_manager_comment(existing.keycrm_lead_id, note)
        except Exception:
            log.warning(
                "ingest.repeat_response_comment_failed",
                candidate_id=existing.id,
                keycrm_lead_id=existing.keycrm_lead_id,
            )
            return
        if existing.status in (CandidateStatus.CLOSED, CandidateStatus.UNREACHABLE):
            log.info(
                "ingest.repeat_response_reactivated",
                candidate_id=existing.id,
                from_status=existing.status,
            )
            existing.status = CandidateStatus.MANAGER_REVIEW
            try:
                await self._keycrm.move_to_status(
                    existing.keycrm_lead_id, route.keycrm_status_id or STATUS_NEW
                )
            except Exception:
                log.warning(
                    "ingest.repeat_response_stage_move_failed",
                    candidate_id=existing.id,
                    keycrm_lead_id=existing.keycrm_lead_id,
                )
        else:
            log.info(
                "ingest.repeat_response_noted",
                candidate_id=existing.id,
                status=existing.status,
            )

    async def _has_live_card(self, existing: Candidate) -> bool:
        """Does this row still point at a card that exists, in any funnel?

        Any funnel, unlike the intake-only check below: a card of a vacancy Єва
        works moves on to later funnels as the person progresses, and that is
        still their card. Only a missing id or a deleted card is "no card". A
        CRM we cannot reach counts as a live card — a duplicate is worse than a
        comment on a card that may be gone.
        """
        if not existing.keycrm_lead_id:
            return False
        try:
            return await self._keycrm.card_pipeline(int(existing.keycrm_lead_id)) is not None
        except Exception:
            return True

    async def ingest(self, payload: IngestPayload) -> IngestResult:
        route = vacancies.get(payload.vacancy_key)

        # Last line of defence against duplicating another system's funnel. The
        # pullers already skip these vacancies; this catches anything that slips
        # through a manual call or a stale env allowlist.
        if vacancies.intake_blocked(route):
            log.info(
                "ingest.vacancy_intake_disabled",
                vacancy=route.key,
                name=payload.full_name,
            )
            return IngestResult(accepted=False, reason=f"intake_disabled: {route.key}")

        phone = normalize_phone(payload.phone_raw)
        hidden_phone = False
        if not phone and payload.no_phone_key:
            phone, hidden_phone = payload.no_phone_key[:20], True
        if not phone:
            return IngestResult(accepted=False, reason="invalid_phone")

        region = normalize_region(payload.region_raw or "")

        # Screening gates belong to the vacancies Єва actually calls, and only to
        # candidates Єва is actively sourcing -- not to someone who applied
        # himself. 2026-09-04: a self-applied candidate (is_response) always
        # gets a card; region/name only decide whether Єва calls a SOURCED
        # candidate, never whether a RESPONSE gets into the CRM at all. An
        # intake-only vacancy (e.g. «Бухгалтер») has its own geo and its own
        # portrait, and a human works the card — filtering here would silently
        # drop people the recruiter wants to see.
        if screening_applies(route, payload):
            if region and not is_region_allowed(
                region, self._settings.regions_allowed, self._settings.regions_blocked
            ):
                return IngestResult(accepted=False, reason=f"region_blocked: {region}")

            # Name-origin gate: only Ukrainian/Slavic candidates go into auto-dial (cyrillic
            # or latin alike). Foreign-origin or uncertain names are skipped BEFORE any card
            # or call — no card, no dial, on to the next candidate. Doubt → skip.
            if not await is_slavic_name(payload.full_name):
                log.info("ingest.name_skipped", name=payload.full_name, phone=phone)
                return IngestResult(accepted=False, reason="name_not_slavic")

        # Local dedup. `phone_e164` is unique, so one person is one row no matter
        # how many vacancies they apply to.
        reused_existing = False
        repeat_of: int | None = None
        async with session_scope() as session:
            existing = (
                await session.execute(select(Candidate).where(Candidate.phone_e164 == phone))
            ).scalar_one_or_none()
            if existing and hidden_phone:
                # Keyed by the board's own application id: seeing it again is the
                # same application, not a repeat one.
                return IngestResult(
                    accepted=True,
                    duplicate=True,
                    candidate_id=existing.id,
                    keycrm_lead_id=existing.keycrm_lead_id,
                    reason="same_application",
                )
            if existing:
                merged_source = merge_sources(existing.source, payload.source)
                if merged_source != existing.source:
                    existing.source = merged_source
                # For a vacancy Єва works, a known phone means we are done — she
                # is already handling this person. For an intake-only vacancy the
                # card lives in a DIFFERENT funnel worked by a different person,
                # so having met this phone before must not stop us; fall through
                # and let the per-funnel CRM check decide.
                if payload.source in ALWAYS_NEW_CARD_SOURCES:
                    # Fall through to card creation below. The row is reused
                    # (one human, one row); the CARD is new, carrying this
                    # submission's own answers in resume_text and the manager
                    # comment -- which the repeat-comment path silently dropped.
                    log.info(
                        "ingest.repeat_gets_own_card",
                        candidate_id=existing.id,
                        source=payload.source,
                        existing_lead_id=existing.keycrm_lead_id,
                    )
                elif payload.is_response:
                    # 28.09.2026, the client (agreed with the recruiter): every
                    # application lands in «Новий» as its own card, repeats too —
                    # even several on one day. No first/second/repeat sorting; the
                    # previous card's number is only mentioned in the new card.
                    # This path used to hang on route.calls_enabled; with calls off
                    # it fell to the plain duplicate return below and vanished.
                    previous_lead = int(existing.keycrm_lead_id or 0)
                    log.info(
                        "ingest.repeat_gets_own_card",
                        candidate_id=existing.id,
                        source=payload.source,
                        existing_lead_id=previous_lead or None,
                    )
                    repeat_of = previous_lead or None
                    existing.keycrm_lead_id = None
                    existing.status = intake_status(route, payload)
                    if payload.vacancy_key:
                        existing.vacancy_key = payload.vacancy_key
                elif route.calls_enabled:
                    if not payload.is_response or await self._has_live_card(existing):
                        # 07.09.2026: this used to return here in total silence --
                        # a repeat robota.ua/work.ua response from someone we
                        # already have a card for vanished with no trace (the
                        # Таран Максим case). A recruiter who had already closed
                        # or given up on a candidate had no way to learn they came
                        # back, short of manually re-checking robota.ua by hand.
                        # Comment always, since that costs nothing and is never
                        # wrong; reopen the card only out of a state we had
                        # already given up on (closed/unreachable) -- a card the
                        # recruiter is actively working must not be yanked out
                        # from under them just because the same person reapplied.
                        if payload.is_response:
                            await self._note_repeat_response(existing, payload, route)
                        return IngestResult(
                            accepted=True,
                            duplicate=True,
                            candidate_id=existing.id,
                            keycrm_lead_id=existing.keycrm_lead_id,
                            reason="local_duplicate",
                        )
                    # 24.09.2026: a response from someone we hold WITHOUT a card
                    # -- ingested while deferral was on, found by cold sourcing
                    # and still waiting for Єва, whose card creation failed, or
                    # whose card a recruiter deleted -- used to take the return
                    # above and never reach the CRM. Fall through to a new card.
                    log.info(
                        "ingest.repeat_response_card_missing",
                        candidate_id=existing.id,
                        old_lead_id=existing.keycrm_lead_id,
                        status=existing.status,
                    )
                    existing.keycrm_lead_id = None
                    if existing.status in (CandidateStatus.CLOSED, CandidateStatus.UNREACHABLE):
                        # A fresh card on a given-up row would be swept straight
                        # back to «Не актуально» by the unreachable give-up job.
                        existing.status = intake_status(route, payload)
                # A card we already made is the one reliable duplicate signal we
                # have: KeyCRM cannot filter cards by phone at all (that endpoint
                # answers 400 — see find_lead_by_phone). But the id has to be
                # CHECKED, not trusted: recruiters delete cards from the UI, and a
                # stale id used to mean "already handled" forever, which hid 21
                # accountants from the funnel on 2026-08-05.
                stale_lead_id = int(existing.keycrm_lead_id or 0)
                if stale_lead_id and payload.source not in ALWAYS_NEW_CARD_SOURCES:
                    try:
                        pid = await self._keycrm.card_pipeline(stale_lead_id)
                    except Exception:
                        # Fail closed: an unreachable CRM must not be read as
                        # "the card is gone" — that way lies duplicates again.
                        return IngestResult(
                            accepted=False,
                            candidate_id=existing.id,
                            reason="card_check_unavailable",
                        )
                    if pid == route.keycrm_pipeline_id:
                        return IngestResult(
                            accepted=True,
                            duplicate=True,
                            candidate_id=existing.id,
                            keycrm_lead_id=stale_lead_id,
                            reason="local_duplicate",
                        )
                    # Deleted, or living in another funnel that this recruiter
                    # never opens. Either way they need a card here.
                    log.info(
                        "ingest.stale_card_reissue",
                        candidate_id=existing.id,
                        old_lead_id=stale_lead_id,
                        found_in_pipeline=pid,
                        vacancy=route.key,
                    )
                    existing.keycrm_lead_id = None
                reused_existing = True
                new_candidate_id = existing.id

            candidate = None if reused_existing else Candidate(
                full_name=payload.full_name.strip(),
                phone_e164=phone,
                email=(payload.email or "").lower() or None,
                region=region or None,
                desired_position=payload.desired_position,
                experience_years=payload.experience_years,
                languages=payload.languages,
                work_ua_url=payload.work_ua_url,
                resume_text=payload.resume_text,
                source=payload.source,
                match_score=payload.match_score,
                vacancy_id=payload.vacancy_id,
                # The routing decision the puller already made, persisted instead of
                # discarded. `vacancy_id` is a constant and cannot carry it.
                vacancy_key=payload.vacancy_key,
                # See intake_status() above for the 2026-09-04 policy change:
                # a self-applied candidate never enters the call queue.
                status=intake_status(route, payload),
            )
            if candidate is not None:
                session.add(candidate)
                await session.flush()
                new_candidate_id = candidate.id

        # Deferred mode: skip KeyCRM entirely at ingest. Orchestrator will
        # create the lead post-call when Єва has a qualified verdict. Only
        # applies where a call actually happens — for an intake-only vacancy
        # deferring would mean the card is never created at all. The same goes
        # for a response: since 04.09 Єва does not call people who applied
        # themselves, so a deferred response card is a card that never comes.
        if route.calls_enabled and not payload.is_response and _defer_keycrm():
            log.info(
                "inbound.local_only",
                candidate_id=new_candidate_id,
                phone=phone[:6] + "***",
                source=payload.source,
            )
            return IngestResult(
                accepted=True,
                candidate_id=new_candidate_id,
                reason="deferred_until_qualified",
            )

        # Eager mode.
        #
        # No branch asks KeyCRM for a phone duplicate here anymore. The only
        # endpoint that could answer that — /pipelines/cards?filter[contact.phone]
        # — rejects the filter outright:
        #
        #   HTTP 400 — Requested filter(s) `contact.phone` are not allowed.
        #   Allowed filter(s) are `pipeline_id, status_id, source_id,
        #   created_between, updated_between`.
        #
        # Verified 2026-08-05, reconfirmed live 2026-09-02 (also tried
        # `client_id` / `buyer_id` — both 400 for the same reason). KeyCRM's
        # buyer/contact search (`find_buyer_by_phone`, /buyer?filter[buyer_phone])
        # does work, but cards cannot be filtered by buyer/client id either, so
        # there is no supported path from "phone" to "does a card exist" on
        # KeyCRM's side. Calling find_lead_by_phone here only ever raised and,
        # combined with fail-closed error handling, silently blocked every
        # single card the moment eager mode ran for a calls-enabled vacancy
        # (e.g. DEFER_KEYCRM_UNTIL_QUALIFIED=0) — the same failure mode the
        # intake-only branch was already exempted from, just never closed here.
        #
        # The local dedup above (candidates.phone_e164, unique) is the only
        # duplicate signal that ever worked and is already authoritative;
        # find_lead_by_phone is kept only as documentation and must not be
        # called from live code — see its docstring in src/common/keycrm.py.


        try:
            _vac_number, _vac_url = vacancy_number_and_url(
                payload.source, payload.board_vacancy_id, route
            )
            created = await self._keycrm.create_lead(
                title=payload.full_name,
                full_name=payload.full_name,
                phone=None if hidden_phone else phone,
                email=payload.email,
                vacancy_name=route.label or payload.vacancy_name,
                vacancy_number=_vac_number,
                vacancy_url=_vac_url,
                workua_response_id=payload.workua_response_id,
                resume_text=payload.resume_text,
                resume_url=payload.work_ua_url,
                manager_comment=_format_manager_comment(payload, region)
                + (f" | 🔁 повторний відгук, попередня картка #{repeat_of}" if repeat_of else "")
                + (" | 📵 телефон прихований на майданчику — відкрийте контакт у кабінеті"
                   if hidden_phone else ""),
                pipeline_id=route.keycrm_pipeline_id or FUNNEL_ID,
                status_id=route.keycrm_status_id or STATUS_NEW,
                # Label the card with the board the person actually came from.
                # Left unset it defaulted to work.ua for everyone, which is how
                # robota.ua applicants became invisible to a recruiter filtering
                # the funnel by source.
                source_id=crm_source_id(payload.source),
                manager_id=DEFAULT_MANAGER_ID,
                # Intake-only cards must look like the sales funnel's: contact
                # present but NOT saved as a client, so the recruiter chooses.
                # 2026-09-03: recruiter wants every new contact to land
                # UNSAVED regardless of vacancy -- she reviews and saves it
                # herself. Used to be route.calls_enabled, which saved sales
                # candidates automatically; that's exactly what she asked to stop.
                save_buyer=False,
            )
            lead_id = int(created.get("id") or 0)
        except Exception as e:
            log.error("keycrm.create_failed", error=str(e))
            return IngestResult(
                accepted=True,
                candidate_id=new_candidate_id,
                reason=f"keycrm_failed:{type(e).__name__}",
            )

        async with session_scope() as session:
            cand_db = await session.get(Candidate, new_candidate_id)
            # Only claim the slot if it is free. A candidate who already has a
            # card from a vacancy Єва works keeps pointing at THAT card — the
            # orchestrator writes call results there. The accountant card is a
            # second card in another funnel, worked by a human, and nothing in
            # our code needs to find it again.
            if cand_db and not cand_db.keycrm_lead_id:
                cand_db.keycrm_lead_id = lead_id

        log.info(
            "inbound.card_created",
            candidate_id=new_candidate_id,
            lead_id=lead_id,
            vacancy=route.key,
            pipeline=route.keycrm_pipeline_id,
            stage=route.keycrm_status_id,
            calls=route.calls_enabled,
        )
        return IngestResult(
            accepted=True,
            candidate_id=new_candidate_id,
            keycrm_lead_id=lead_id,
        )


async def issue_missing_cards(
    router: InboundRouter | None = None,
    *,
    window_hours: int = 48,
    settle_minutes: int = 10,
    limit: int = 20,
) -> int:
    """Cards for applicants who reached our database and not the CRM.

    A failed create comes back as `keycrm_failed`, which counts as accepted: the
    row is written, the card is not, and the board cursor that brought the
    person in has already moved on. Nothing asked KeyCRM a second time.

    Every row picked here is one a card was due for. MANAGER_REVIEW is where a
    response, an intake-only applicant and a qualified call all wait for a human,
    and each of those has a card by then. Sourced candidates waiting for Єва sit
    in NEW_RESUME; a synthetic Telegram key has no number to put on a card. The
    window keeps old, settled rows out. A row touched in the last few minutes
    may be an ingest still waiting on KeyCRM, and taking it now would make a
    second card. Returns the number of cards made.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)
    settled = now - timedelta(minutes=settle_minutes)
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Candidate)
                .where(Candidate.keycrm_lead_id.is_(None))
                .where(Candidate.status == CandidateStatus.MANAGER_REVIEW)
                .where(Candidate.phone_e164.like("+%"))
                .where(Candidate.created_at >= since)
                .where(Candidate.updated_at <= settled)
                .order_by(Candidate.id)
                .limit(limit)
            )
        ).scalars().all()
        # Built inside the session: the rows are detached once it closes.
        todo = [
            IngestPayload(
                full_name=c.full_name,
                phone_raw=c.phone_e164,
                email=c.email,
                region_raw=c.region,
                desired_position=c.desired_position,
                experience_years=c.experience_years,
                work_ua_url=c.work_ua_url,
                resume_text=c.resume_text,
                source=c.source,
                vacancy_id=c.vacancy_id,
                vacancy_key=c.vacancy_key or vacancies.DEFAULT.key,
                # Only people waiting for a human are here, and that is what a
                # response is: no screening, no deferral, a card.
                is_response=True,
            )
            for c in rows
        ]
    if not todo:
        return 0
    router = router or InboundRouter()
    made = 0
    for payload in todo:
        result = await router.ingest(payload)
        if result.keycrm_lead_id:
            made += 1
        log.warning(
            "ingest.missing_card_retry",
            candidate_id=result.candidate_id,
            name=payload.full_name,
            lead_id=result.keycrm_lead_id,
            reason=result.reason,
        )
    return made
