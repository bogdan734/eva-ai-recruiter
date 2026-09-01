"""Inbound lead router.

Two modes controlled by DEFER_KEYCRM_UNTIL_QUALIFIED env (default: on):

  - deferred (default): only writes a local Candidate. KeyCRM lead is
    created later by the orchestrator's post-call `_finalize_call` after
    Єва's screening produces a decision. This keeps the CRM clean —
    only candidates who actually spoke with Єва land in the funnel.

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
from datetime import datetime

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


@dataclass
class IngestResult:
    accepted: bool
    reason: str = ""
    candidate_id: int | None = None
    keycrm_lead_id: int | None = None
    duplicate: bool = False


def _format_manager_comment(payload: IngestPayload, region: str | None) -> str:
    """Pack AI metadata into manager_comment (KeyCRM has no other free-form fields)."""
    bits: list[str] = []
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


class InboundRouter:
    def __init__(self, keycrm: CRMClient | None = None) -> None:
        self._keycrm = keycrm or get_crm()
        self._settings = get_settings()

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
        if not phone:
            return IngestResult(accepted=False, reason="invalid_phone")

        region = normalize_region(payload.region_raw or "")

        # Screening gates belong to the vacancies Єва actually calls. An
        # intake-only vacancy (e.g. «Бухгалтер») has its own geo and its own
        # portrait, and a human works the card — filtering here would silently
        # drop people the recruiter wants to see.
        if route.screen_enabled:
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
        async with session_scope() as session:
            existing = (
                await session.execute(select(Candidate).where(Candidate.phone_e164 == phone))
            ).scalar_one_or_none()
            if existing:
                merged_source = merge_sources(existing.source, payload.source)
                if merged_source != existing.source:
                    existing.source = merged_source
                # For a vacancy Єва works, a known phone means we are done — she
                # is already handling this person. For an intake-only vacancy the
                # card lives in a DIFFERENT funnel worked by a different person,
                # so having met this phone before must not stop us; fall through
                # and let the per-funnel CRM check decide.
                if route.calls_enabled:
                    return IngestResult(
                        accepted=True,
                        duplicate=True,
                        candidate_id=existing.id,
                        keycrm_lead_id=existing.keycrm_lead_id,
                        reason="local_duplicate",
                    )
                # A card we already made is the one reliable duplicate signal we
                # have: KeyCRM cannot filter cards by phone at all (that endpoint
                # answers 400 — see find_lead_by_phone). But the id has to be
                # CHECKED, not trusted: recruiters delete cards from the UI, and a
                # stale id used to mean "already handled" forever, which hid 21
                # accountants from the funnel on 2026-08-05.
                stale_lead_id = int(existing.keycrm_lead_id or 0)
                if stale_lead_id:
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
                # MANAGER_REVIEW keeps intake-only candidates out of the dialer:
                # the dispatcher only picks NEW_RESUME / IN_CALL_QUEUE, and the
                # CRM stage sweep ignores this status too.
                status=(
                    CandidateStatus.NEW_RESUME
                    if route.calls_enabled
                    else CandidateStatus.MANAGER_REVIEW
                ),
            )
            if candidate is not None:
                session.add(candidate)
                await session.flush()
                new_candidate_id = candidate.id

        # Deferred mode: skip KeyCRM entirely at ingest. Orchestrator will
        # create the lead post-call when Єва has a qualified verdict. Only
        # applies where a call actually happens — for an intake-only vacancy
        # deferring would mean the card is never created at all.
        if route.calls_enabled and _defer_keycrm():
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

        # Eager mode — original behaviour.
        #
        # Intake-only vacancies deliberately do NOT ask KeyCRM here. Their
        # duplicate check is the card_pipeline() lookup above, which is
        # authoritative; this endpoint cannot answer a phone question at all
        # (HTTP 400 — see find_lead_by_phone), so calling it would only ever
        # raise and, combined with failing closed, block every single card.
        remote = None
        if route.calls_enabled:
            try:
                remote = await self._keycrm.find_lead_by_phone(phone)
            except Exception as e:
                # FAIL CLOSED. This used to swallow the error and carry on with
                # remote=None — "no duplicate found" — so a rate-limited or
                # broken lookup produced another card for the same person on
                # every poll. Skipping costs a cycle; guessing costs cleanup.
                log.error(
                    "keycrm.dedup_failed_skipping", error=str(e), phone=phone[:6] + "***"
                )
                return IngestResult(
                    accepted=False,
                    candidate_id=new_candidate_id,
                    reason=f"dedup_unavailable:{type(e).__name__}",
                )

        if remote:
            async with session_scope() as session:
                cand_db = await session.get(Candidate, new_candidate_id)
                if cand_db and not cand_db.keycrm_lead_id:
                    cand_db.keycrm_lead_id = int(remote.get("id") or 0)
            return IngestResult(
                accepted=True,
                duplicate=True,
                candidate_id=new_candidate_id,
                keycrm_lead_id=int(remote.get("id") or 0),
                reason="remote_duplicate",
            )

        try:
            created = await self._keycrm.create_lead(
                title=payload.full_name,
                full_name=payload.full_name,
                phone=phone,
                email=payload.email,
                vacancy_name=route.label or payload.vacancy_name,
                workua_response_id=payload.workua_response_id,
                resume_text=payload.resume_text,
                resume_url=payload.work_ua_url,
                manager_comment=_format_manager_comment(payload, region),
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
                save_buyer=route.calls_enabled,
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
