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

from src.common.db import session_scope
from src.common.keycrm import (
    DEFAULT_MANAGER_ID,
    FUNNEL_ID,
    STATUS_NEW,
    KeyCRMClient,
)
from src.common.models import Candidate, CandidateStatus
from src.common.phone import normalize_phone
from src.common.regions import is_region_allowed, normalize_region
from src.common.settings import get_settings


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


class InboundRouter:
    def __init__(self, keycrm: KeyCRMClient | None = None) -> None:
        self._keycrm = keycrm or KeyCRMClient()
        self._settings = get_settings()

    async def ingest(self, payload: IngestPayload) -> IngestResult:
        phone = normalize_phone(payload.phone_raw)
        if not phone:
            return IngestResult(accepted=False, reason="invalid_phone")

        region = normalize_region(payload.region_raw or "")
        if region and not is_region_allowed(
            region, self._settings.regions_allowed, self._settings.regions_blocked
        ):
            return IngestResult(accepted=False, reason=f"region_blocked: {region}")

        # Local dedup
        async with session_scope() as session:
            existing = (
                await session.execute(select(Candidate).where(Candidate.phone_e164 == phone))
            ).scalar_one_or_none()
            if existing:
                if existing.source != payload.source and payload.source not in existing.source:
                    existing.source = f"{existing.source},{payload.source}"
                return IngestResult(
                    accepted=True,
                    duplicate=True,
                    candidate_id=existing.id,
                    keycrm_lead_id=existing.keycrm_lead_id,
                    reason="local_duplicate",
                )

            candidate = Candidate(
                full_name=payload.full_name.strip(),
                phone_e164=phone,
                email=(payload.email or "").lower() or None,
                region=region or None,
                desired_position=payload.desired_position,
                experience_years=payload.experience_years,
                languages=payload.languages,
                work_ua_url=payload.work_ua_url,
                source=payload.source,
                match_score=payload.match_score,
                vacancy_id=payload.vacancy_id,
                status=CandidateStatus.NEW_RESUME,
            )
            session.add(candidate)
            await session.flush()
            new_candidate_id = candidate.id

        # Deferred mode: skip KeyCRM entirely at ingest. Orchestrator will
        # create the lead post-call when Єва has a qualified verdict.
        if _defer_keycrm():
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
        try:
            remote = await self._keycrm.find_lead_by_phone(phone)
        except Exception as e:
            log.warning("keycrm.dedup_failed", error=str(e))
            remote = None

        if remote:
            async with session_scope() as session:
                cand_db = await session.get(Candidate, new_candidate_id)
                if cand_db:
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
                vacancy_name=payload.vacancy_name,
                workua_response_id=payload.workua_response_id,
                resume_text=payload.resume_text,
                resume_url=payload.work_ua_url,
                manager_comment=_format_manager_comment(payload, region),
                pipeline_id=FUNNEL_ID,
                status_id=STATUS_NEW,
                manager_id=DEFAULT_MANAGER_ID,
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
            if cand_db:
                cand_db.keycrm_lead_id = lead_id

        return IngestResult(
            accepted=True,
            candidate_id=new_candidate_id,
            keycrm_lead_id=lead_id,
        )
