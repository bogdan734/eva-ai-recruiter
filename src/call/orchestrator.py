"""End-to-end call orchestration glue.

Wires together: Scheduler picks candidate → builds Vapi assistant overrides → dispatches
call via Vapi → on end-of-call webhook (handled in api/services.py) the post-call
summarizer runs → KeyCRM card updated → candidate state advances.

This module covers both outbound (initiated by scheduler) and inbound
(candidate dials the Vapi number directly).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import select

from src.api.inbound_router import IngestPayload, InboundRouter
from src.call.script_template import render_system_prompt
from src.call.summarizer import CallSummary, Summarizer
from src.call.vapi_client import VapiClient
from src.common.db import session_scope
from src.common.keycrm import KeyCRMClient
from src.common.keycrm_fields import STAGE_MAP
from src.common.models import Call, CallStatus, Candidate, CandidateStatus, Vacancy
from src.common.phone import normalize_phone
from src.common.settings import get_settings

log = structlog.get_logger()


class CallOrchestrator:
    def __init__(
        self,
        vapi: VapiClient | None = None,
        keycrm: KeyCRMClient | None = None,
        summarizer: Summarizer | None = None,
        inbound_router: InboundRouter | None = None,
    ) -> None:
        self._vapi = vapi or VapiClient()
        self._keycrm = keycrm or KeyCRMClient()
        self._summarizer = summarizer or Summarizer()
        self._inbound_router = inbound_router or InboundRouter(keycrm=self._keycrm)
        self._settings = get_settings()

    async def dispatch_for_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        async with session_scope() as session:
            candidate = await session.get(Candidate, candidate_id)
            if not candidate:
                log.warning("orchestrator.candidate_missing", id=candidate_id)
                return None
            vacancy = (
                await session.get(Vacancy, candidate.vacancy_id) if candidate.vacancy_id else None
            )

            candidate.call_attempts += 1
            candidate.status = CandidateStatus.CALLING
            attempt_no = candidate.call_attempts

        prompt = render_system_prompt(
            candidate_name=candidate.full_name,
            candidate_phone=candidate.phone_e164,
            candidate_position=candidate.desired_position or "",
            source=candidate.source,
            vacancy_title=vacancy.title if vacancy else "",
            vacancy_pitch=(vacancy.description.split("\n", 1)[0] if vacancy else ""),
            vacancy_requirements=(vacancy.description if vacancy else ""),
            vacancy_salary=(
                f"{vacancy.salary_min}-{vacancy.salary_max} грн"
                if vacancy and vacancy.salary_min
                else "обговорюється з менеджером"
            ),
            vacancy_location=(vacancy.region if vacancy and vacancy.region else "Україна"),
        )

        overrides: dict[str, Any] = {
            # provider+model are REQUIRED by Vapi whenever model is overridden;
            # sending messages alone returns HTTP 400 and the call never happens.
            "model": {
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "messages": [{"role": "system", "content": prompt}],
            },
            "metadata": {
                "candidate_id": candidate.id,
                "vacancy_id": candidate.vacancy_id,
                "attempt": attempt_no,
            },
        }

        try:
            call = await self._vapi.create_outbound_call(
                assistant_id=self._settings.vapi_assistant_id,
                phone_number_id=self._settings.vapi_phone_number_id,
                customer_number_e164=candidate.phone_e164,
                assistant_overrides=overrides,
                metadata={"candidate_id": candidate.id},
            )
        except Exception as e:  # prompt render or dispatch failed
            log.error("orchestrator.dispatch_failed", error=str(e), id=candidate.id)
            # failed to place the call -> put candidate back in the queue instead
            # of leaving it stuck in CALLING forever
            async with session_scope() as session:
                cand = await session.get(Candidate, candidate.id)
                if cand and cand.status == CandidateStatus.CALLING:
                    cand.status = CandidateStatus.IN_CALL_QUEUE
                    cand.call_attempts = max(0, cand.call_attempts - 1)
            return None

        async with session_scope() as session:
            db_call = Call(
                candidate_id=candidate.id,
                vapi_call_id=str(call.get("id", "")),
                attempt_number=attempt_no,
                started_at=datetime.utcnow(),
                status=CallStatus.FAILED,
            )
            session.add(db_call)
        log.info("orchestrator.dispatched", id=candidate.id, vapi_id=call.get("id"))
        return call

    async def process_end_of_call(
        self, *, vapi_call_id: str, transcript: str, duration_sec: float, recording_url: str | None
    ) -> None:
        async with session_scope() as session:
            db_call = (
                await session.execute(select(Call).where(Call.vapi_call_id == vapi_call_id))
            ).scalar_one_or_none()
            if not db_call:
                log.warning("orchestrator.unknown_call", vapi_id=vapi_call_id)
                return
            candidate = await session.get(Candidate, db_call.candidate_id)
            vacancy = (
                await session.get(Vacancy, candidate.vacancy_id)
                if candidate and candidate.vacancy_id
                else None
            )
            await self._finalize_call(
                db_call=db_call,
                candidate=candidate,
                vacancy=vacancy,
                transcript=transcript,
                duration_sec=duration_sec,
                recording_url=recording_url,
            )

    async def process_inbound_call(
        self,
        *,
        vapi_call_id: str,
        caller_phone: str,
        transcript: str,
        duration_sec: float,
        recording_url: str | None,
    ) -> None:
        phone = normalize_phone(caller_phone) or caller_phone
        if not phone:
            log.warning("orchestrator.inbound.no_phone", vapi_id=vapi_call_id)
            return

        async with session_scope() as session:
            existing_call = (
                await session.execute(select(Call).where(Call.vapi_call_id == vapi_call_id))
            ).scalar_one_or_none()
            if existing_call:
                log.info(
                    "orchestrator.inbound.already_processed", vapi_id=vapi_call_id
                )
                return

        candidate_id: int | None = None
        async with session_scope() as session:
            existing = (
                await session.execute(
                    select(Candidate).where(Candidate.phone_e164 == phone)
                )
            ).scalar_one_or_none()
            if existing:
                candidate_id = existing.id

        if candidate_id is None:
            try:
                last4 = phone[-4:]
            except Exception:
                last4 = "0000"
            placeholder_name = f"Inbound caller {last4}"
            result = await self._inbound_router.ingest(
                IngestPayload(
                    full_name=placeholder_name,
                    phone_raw=phone,
                    source="inbound_call",
                )
            )
            if not result.accepted:
                log.warning(
                    "orchestrator.inbound.ingest_rejected",
                    phone=phone[:6] + "***",
                    reason=result.reason,
                )
                return
            candidate_id = result.candidate_id

        if candidate_id is None:
            log.warning("orchestrator.inbound.no_candidate", vapi_id=vapi_call_id)
            return

        async with session_scope() as session:
            candidate = await session.get(Candidate, candidate_id)
            if not candidate:
                log.warning("orchestrator.inbound.candidate_missing", id=candidate_id)
                return
            candidate.call_attempts += 1
            attempt_no = candidate.call_attempts
            db_call = Call(
                candidate_id=candidate.id,
                vapi_call_id=vapi_call_id,
                attempt_number=attempt_no,
                started_at=datetime.utcnow(),
                status=CallStatus.FAILED,
            )
            session.add(db_call)
            await session.flush()
            db_call_id = db_call.id

        async with session_scope() as session:
            db_call = await session.get(Call, db_call_id)
            candidate = await session.get(Candidate, candidate_id)
            vacancy = (
                await session.get(Vacancy, candidate.vacancy_id)
                if candidate and candidate.vacancy_id
                else None
            )
            await self._finalize_call(
                db_call=db_call,
                candidate=candidate,
                vacancy=vacancy,
                transcript=transcript,
                duration_sec=duration_sec,
                recording_url=recording_url,
            )
        log.info(
            "orchestrator.inbound.processed",
            vapi_id=vapi_call_id,
            candidate_id=candidate_id,
        )

    async def _finalize_call(
        self,
        *,
        db_call: Call,
        candidate: Candidate | None,
        vacancy: Vacancy | None,
        transcript: str,
        duration_sec: float,
        recording_url: str | None,
    ) -> None:
        summary: CallSummary
        try:
            summary = await self._summarizer.summarize(
                transcript=transcript,
                vacancy_title=vacancy.title if vacancy else None,
                vacancy_requirements=vacancy.description if vacancy else None,
            )
        except Exception as e:
            log.error("orchestrator.summary_failed", error=str(e))
            summary = CallSummary(
                summary="",
                sentiment="neutral",
                objections=["none"],
                language="uk",
                qualified=False,
            )

        db_call.ended_at = datetime.utcnow()
        db_call.duration_sec = int(duration_sec)
        db_call.audio_url = recording_url
        db_call.transcript = transcript
        db_call.ai_summary = summary.summary
        db_call.sentiment = summary.sentiment
        db_call.objections = summary.objections
        db_call.language_used = summary.language
        db_call.tokens_input += summary.tokens_in
        db_call.tokens_output += summary.tokens_out
        # Age gate: the voice model may misjudge the window, so enforce it here —
        # otherwise an out-of-window candidate reaches KeyCRM as qualified.
        if summary.qualified and summary.candidate_age:
            s_cfg = self._settings
            age_lo = min(s_cfg.profile_age_min_f, s_cfg.profile_age_min_m)
            age_hi = max(s_cfg.profile_age_max_f, s_cfg.profile_age_max_m)
            if not (age_lo <= summary.candidate_age <= age_hi):
                log.info(
                    "orchestrator.age_gate_reject",
                    age=summary.candidate_age,
                    window=f"{age_lo}-{age_hi}",
                    call_id=db_call.id,
                )
                summary.qualified = False

        db_call.status = CallStatus.SUCCESS if summary.qualified else CallStatus.HANGUP

        # Cold-base promise fulfilment: Єва pledged to send the anketa in Telegram.
        if summary.needs_anketa and candidate and candidate.phone_e164:
            try:
                async with httpx.AsyncClient(timeout=20) as tg:
                    r = await tg.post(
                        f"{self._settings.tguserbot_url}/send_form",
                        json={
                            "phone": candidate.phone_e164,
                            "name": candidate.full_name or "",
                        },
                    )
                    log.info(
                        "orchestrator.anketa_form",
                        status=r.status_code,
                        resp=r.text[:200],
                        candidate_id=candidate.id,
                    )
            except Exception as e:
                log.warning("orchestrator.anketa_form_failed", error=str(e))

        if candidate:
            if summary.qualified:
                candidate.status = CandidateStatus.MANAGER_REVIEW
                new_stage = STAGE_MAP.get("manager_review")
            elif summary.spoke_with_candidate:
                # Screening actually happened and the candidate did not fit —
                # close them out. Re-dialing someone who already answered the
                # questions annoys people and burns minutes.
                candidate.status = CandidateStatus.CLOSED
                new_stage = STAGE_MAP.get("closed")
            elif candidate.call_attempts >= self._settings.call_max_attempts:
                candidate.status = CandidateStatus.UNREACHABLE
                new_stage = STAGE_MAP.get("unreachable")
            else:
                candidate.status = CandidateStatus.IN_CALL_QUEUE
                # Someone we already tried is "В роботі"; untouched people stay
                # in "Новий" so the working stage only holds live cases.
                new_stage = STAGE_MAP.get(
                    "in_call_queue" if candidate.call_attempts else "new_resume"
                )

            # Deferred-KeyCRM mode: create the lead now that we have a
            # verdict, then set the stage to match. Skip creation on clear
            # non-signals (no answer + retry left) — that just retries later.
            if not candidate.keycrm_lead_id:
                should_push = (
                    summary.qualified
                    or summary.spoke_with_candidate  # screened, recruiter should see it
                    or candidate.call_attempts >= self._settings.call_max_attempts
                )
                if should_push:
                    try:
                        created = await self._keycrm.create_lead(
                            title=candidate.full_name,
                            full_name=candidate.full_name,
                            phone=candidate.phone_e164,
                            email=candidate.email,
                            vacancy_name=(vacancy.title if vacancy else "Менеджер з продажу"),
                            manager_comment=(
                                f"джерело: {candidate.source} | "
                                f"AI verdict: {'qualified' if summary.qualified else 'unreached'} | "
                                f"attempts: {candidate.call_attempts} | "
                                f"summary: {(summary.summary or '')[:400]}"
                            ),
                        )
                        candidate.keycrm_lead_id = int(created.get("id") or 0) or None
                    except Exception as e:
                        log.warning(
                            "orchestrator.keycrm_create_failed",
                            error=str(e),
                            candidate_id=candidate.id,
                        )

            # Flag the lead as AI-handled by making "Єва АІ" the responsible
            # manager — recruiters can then spot these in the list at a glance.
            ai_manager = self._settings.keycrm_ai_manager_id
            if candidate.keycrm_lead_id and ai_manager:
                try:
                    await self._keycrm.assign_manager(candidate.keycrm_lead_id, ai_manager)
                except Exception as e:
                    log.warning("orchestrator.keycrm_assign_failed", error=str(e))

            # Put the conversation itself on the card — summary, transcript,
            # recording — otherwise the recruiter opens a lead with no context.
            if candidate.keycrm_lead_id:
                try:
                    # Vapi's recordingUrl needs S3 auth and its presigned links
                    # expire within the hour — link to our own redirect instead.
                    playable = (
                        f"{self._settings.app_base_url.rstrip('/')}/recordings/{db_call.vapi_call_id}"
                        if db_call.vapi_call_id
                        else recording_url
                    )
                    await self._keycrm.write_call_results(
                        candidate.keycrm_lead_id,
                        summary=summary.summary,
                        transcript=transcript,
                        audio_url=playable,
                        region=candidate.region,
                        match_score=candidate.match_score,
                    )
                    await self._keycrm.append_manager_comment(
                        candidate.keycrm_lead_id,
                        f"дзвінок {datetime.utcnow():%d.%m %H:%M} UTC | "
                        f"{int(duration_sec)}с | {summary.sentiment} | "
                        f"{'КВАЛІФІКОВАНИЙ' if summary.qualified else 'не кваліфікований'}"
                        + (f" | вік {summary.candidate_age}" if summary.candidate_age else "")
                        + f"\n{(summary.summary or '')[:1500]}",
                    )
                except Exception as e:
                    log.warning("orchestrator.keycrm_results_failed", error=str(e))

            if candidate.keycrm_lead_id and new_stage is not None:
                try:
                    await self._keycrm.move_to_status(
                        candidate.keycrm_lead_id, new_stage
                    )
                except Exception as e:
                    # A missing card almost always means a recruiter deleted it on
                    # purpose. Do NOT recreate it — that would drag rejected people
                    # back into the pipeline. Close the candidate instead.
                    log.warning(
                        "orchestrator.keycrm_card_missing",
                        error=str(e),
                        candidate_id=candidate.id,
                        lead_id=candidate.keycrm_lead_id,
                    )
                    candidate.status = CandidateStatus.CLOSED
