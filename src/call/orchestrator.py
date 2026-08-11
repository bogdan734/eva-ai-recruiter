"""End-to-end call orchestration glue.

Wires together: Scheduler picks candidate → builds Vapi assistant overrides → dispatches
call via Vapi → on end-of-call webhook (handled in api/services.py) the post-call
summarizer runs → KeyCRM card updated → candidate state advances.

This module covers both outbound (initiated by scheduler) and inbound
(candidate dials the Vapi number directly).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog
import os

from sqlalchemy import func, select

from src.api.inbound_router import IngestPayload, InboundRouter
from src.call import line_health
from src.call.script_template import render_system_prompt
from src.call.summarizer import CallSummary, Summarizer
from src.call.vapi_client import VapiClient
from src.common.db import session_scope
from src.common.crm import CRMClient, get_crm
from src.common.keycrm_fields import STAGE_MAP
from src.common.models import Call, CallStatus, Candidate, CandidateStatus, Vacancy
from src.common.phone import normalize_phone
from src.common import sources as _sources
from src.common.settings import get_settings
from src.common import vacancies as _vacancies
from src.common import vacancy_store as _vacancy_store

log = structlog.get_logger()


# Never call one person more than this, ever (mirrors the dispatcher's guard — the
# orchestrator needs it to close people out once the budget is spent).
HARD_CALL_CAP = int(os.environ.get("HARD_CALL_CAP", "6"))


# Vapi endedReason / SIP code -> what a recruiter actually needs to read. The raw
# strings ("error-sip-outbound-call-failed-to-connect") mean nothing on a card.
_REASON_HUMAN = {
    "customer-busy": "зайнято",
    "customer-did-not-answer": "не відповів",
    "customer-did-not-give-microphone-permission": "не відповів",
    "voicemail": "автовідповідач",
    "assistant-said-end-call-phrase": "розмова завершена",
    "assistant-ended-call": "розмова завершена",
    "customer-ended-call": "кандидат поклав слухавку",
    "silence-timed-out": "тиша в лінії",
    "pipeline-error": "технічний збій",
}


def _reason_human(reason: str | None) -> str:
    """Plain-Ukrainian reason. SIP 480 is NOT a line fault — the operator confirmed it
    means the subscriber cannot receive calls (no credit / abroad)."""
    if not reason:
        return "—"
    r = str(reason).lower()
    for key, human in _REASON_HUMAN.items():
        if key in r:
            return human
    if "failed-to-connect" in r or "480" in r:
        return "абонент недоступний (немає коштів / за кордоном)"
    if "503" in r or "403" in r:
        return "збій оператора"
    return str(reason)[:48]


def _wants_callback(summary) -> bool:
    """Did the candidate ask us to call back (or promise to call us)?"""
    if getattr(summary, "best_callback_time", None):
        return True
    text = f"{getattr(summary, 'summary', '') or ''}".lower()
    return any(k in text for k in (
        "передзвон", "перетелефон", "зателефонуйте пізніше",
        "зайнят", "не можу говорити", "передзвоню",
    ))


def _callback_moment(summary, tz) -> datetime:
    """When to ring back: what the candidate named, else 11:00 next day."""
    raw = getattr(summary, "best_callback_time", None)
    if raw:
        try:
            when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=tz)
            if when > datetime.now(tz):
                return when
        except Exception:
            pass
    nxt = datetime.now(tz) + timedelta(days=1)
    return nxt.replace(hour=11, minute=0, second=0, microsecond=0)


def _activity_note(
    *,
    calls_total: int,
    talked_sec: int,
    tg_sent: bool,
    tg_error: str | None,
    label: str,
    last_reason: str | None = None,
) -> str:
    """The 'what Eva actually did' block for the CRM card.

    Recruiters could not see any of this before — the card only said "поговорили,
    добираємо в Telegram" while the person had 13 unanswered calls and no message
    ever went out. Everything here must be readable at a glance, no digging.
    """
    tg = "✅ надіслано" if tg_sent else f"❌ НЕ надіслано ({tg_error or 'немає в Telegram'})"
    talked = f"{talked_sec}с" if talked_sec else "не говорили"
    return (
        "🤖 ЄВА — ЩО ЗРОБЛЕНО\n"
        f"• Дзвінків: {calls_total} (ліміт {HARD_CALL_CAP})\n"
        f"• Останній результат: {_reason_human(last_reason)}\n"
        f"• Найдовша розмова: {talked}\n"
        f"• Telegram: {tg}\n"
        f"• Підсумок: {label}"
    )


def _outcome_label(status: "CandidateStatus", summary, tg_sent: bool = False) -> str:
    """Short human label of the call result for the contact (buyer) note. tg_sent tells
    the truth about the Telegram fallback so we never claim a message that never went."""
    reason = getattr(summary, "reject_reason", "none")
    if status == CandidateStatus.MANAGER_REVIEW:
        return "✅ кваліфікований → В роботі"
    if status == CandidateStatus.CALL_DONE:
        return "🔵 поговорили, дотискаємо в Telegram" if tg_sent else "🔵 поговорили (у Telegram недоступний)"
    if status == CandidateStatus.UNREACHABLE:
        return "📵 недозвон, написали в Telegram" if tg_sent else "📵 недозвон (у Telegram недоступний)"
    if status == CandidateStatus.CLOSED:
        if getattr(summary, "time_waster", False) or reason == "misbehaved":
            return "❌ не підходить"
        if reason == "not_target":
            return "🚫 не ЦА"
        return "⚪ не актуально"
    return "☎️ опрацьовано"


class CallOrchestrator:
    def __init__(
        self,
        vapi: VapiClient | None = None,
        keycrm: CRMClient | None = None,
        summarizer: Summarizer | None = None,
        inbound_router: InboundRouter | None = None,
    ) -> None:
        self._vapi = vapi or VapiClient()
        self._keycrm = keycrm or get_crm()
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
            # Consume the scheduled callback — otherwise a due callback_at would keep
            # re-qualifying this candidate on every slot.
            candidate.callback_at = None
            attempt_no = candidate.call_attempts
            # On a callback, resume instead of starting cold if a previous call actually
            # connected and the candidate spoke (dropped mid-conversation). A pure
            # no-answer leaves no transcript, so this stays a plain redial.
            resume_transcript = ""
            if attempt_no >= 2:
                prev = (await session.execute(
                    select(Call)
                    .where(Call.candidate_id == candidate_id, Call.transcript.is_not(None))
                    .order_by(Call.id.desc())
                )).scalars().first()
                if prev and prev.transcript and len(prev.transcript.strip()) > 40:
                    resume_transcript = prev.transcript.strip()[:1500]

        # Which posting did this person answer? `candidate.vacancy_key` carries it
        # now; NULL means the default vacancy, which is what every row created
        # before 08.08 is. The spoken fields come from that vacancy — panel edit
        # first, shipped default next, global .env last — so two vacancies can be
        # pitched differently instead of everyone hearing one text.
        route = _vacancies.get(candidate.vacancy_key)
        spoken = _vacancy_store.spoken
        prompt = render_system_prompt(
            candidate_name=candidate.full_name,
            candidate_phone=candidate.phone_e164,
            candidate_position=candidate.desired_position or "",
            source=candidate.source,
            company_pitch=spoken(route, "spoken_pitch") or None,
            vacancy_schedule=spoken(route, "spoken_schedule") or None,
            vacancy_benefits=spoken(route, "spoken_benefits") or None,
            vacancy_title=spoken(route, "spoken_title") or (vacancy.title if vacancy else ""),
            vacancy_pitch=(vacancy.description.split("\n", 1)[0] if vacancy else ""),
            vacancy_requirements=(vacancy.description if vacancy else ""),
            vacancy_salary=(
                spoken(route, "spoken_salary")
                or (
                    f"{vacancy.salary_min}-{vacancy.salary_max} грн"
                    if vacancy and vacancy.salary_min
                    else "обговорюється з менеджером"
                )
            ),
            vacancy_location=(vacancy.region if vacancy and vacancy.region else "Україна"),
        )

        resume_first: dict[str, Any] = {}
        if resume_transcript:
            prompt += (
                "\n\n[ПОВТОРНИЙ ДЗВІНОК — попередня розмова обірвалася на півслові. "
                "Ось що вже встигли обговорити:\n" + resume_transcript +
                "\nКоротко нагадай про це і продовж З ТОГО МІСЦЯ — не починай анкету "
                "спочатку, не повторюй уже поставлені питання.]"
            )
            resume_first = {
                "firstMessage": "Доброго дня! Ми з вами нещодавно почали розмову, але "
                "звʼязок обірвався. Зручно продовжити?",
                "firstMessageMode": "assistant-speaks-first",
            }

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
            **resume_first,
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
        self, *, vapi_call_id: str, transcript: str, duration_sec: float,
        recording_url: str | None, ended_reason: str | None = None
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
                ended_reason=ended_reason,
            )

    async def process_inbound_call(
        self,
        *,
        vapi_call_id: str,
        caller_phone: str,
        transcript: str,
        duration_sec: float,
        recording_url: str | None,
        ended_reason: str | None = None,
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
                ended_reason=ended_reason,
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
        ended_reason: str | None = None,
    ) -> None:
        if ended_reason:
            db_call.ended_reason = str(ended_reason)[:64]

        # Who failed — the carrier, the number, or the candidate? Everything below
        # this point assumes the phone actually rang, and for six days in August it
        # had not: Stream Telecom refused every call with SIP 403 and the dispatcher
        # quietly spent an attempt on each one until twelve real candidates were
        # filed as `unreachable`. Settle fault first.
        fault = line_health.classify(ended_reason)
        if fault == "provider_fault":
            phone = candidate.phone_e164 if candidate else None
            db_call.ended_at = datetime.utcnow()
            db_call.duration_sec = int(duration_sec)
            db_call.status = CallStatus.FAILED
            if candidate is not None:
                # Give the attempt back. The candidate never got a ring, so holding
                # it against them — and eventually filing them as unreachable — is
                # simply wrong. Same rollback the place-call failure path does.
                async with session_scope() as sess:
                    cand = await sess.get(Candidate, candidate.id)
                    if cand:
                        cand.call_attempts = max(0, cand.call_attempts - 1)
                        if cand.status == CandidateStatus.CALLING:
                            cand.status = CandidateStatus.IN_CALL_QUEUE
            log.warning(
                "orchestrator.provider_fault",
                candidate_id=getattr(candidate, "id", None),
                call_id=db_call.id, reason=ended_reason,
            )
            # No CRM write, no Telegram fallback: nothing happened that the
            # recruiter or the candidate should ever hear about.
            if line_health.record_provider_fault(phone, ended_reason):
                line_health.pause_calls(f"SIP fault streak: {ended_reason}")
                await line_health.alert_admins(
                    "🔴 <b>Обдзвін зупинено автоматично</b>\n\n"
                    f"Оператор відхиляє вихідні: <code>{ended_reason}</code>\n"
                    "Поспіль невдалих дзвінків на різні номери — телефони "
                    "кандидатів не дзвонили взагалі.\n\n"
                    "Спроби кандидатам НЕ зараховані, черга збережена.\n"
                    "Після відповіді оператора зняти паузу через /menu ▶️"
                )
            return
        if fault == "dead_number" and candidate is not None:
            # The network says there is no such subscriber. Redialling produces the
            # same answer every time, so spend nothing more on it.
            async with session_scope() as sess:
                cand = await sess.get(Candidate, candidate.id)
                if cand:
                    cand.status = CandidateStatus.CLOSED
            db_call.ended_at = datetime.utcnow()
            db_call.duration_sec = int(duration_sec)
            db_call.status = CallStatus.FAILED
            log.info(
                "orchestrator.dead_number",
                candidate_id=candidate.id, phone=candidate.phone_e164, reason=ended_reason,
            )
            try:
                if candidate.keycrm_lead_id:
                    await self._keycrm.move_to_status(
                        candidate.keycrm_lead_id, STAGE_MAP.get("not_actual")
                    )
                    await self._keycrm.append_manager_comment(
                        candidate.keycrm_lead_id,
                        f"Номер не існує в мережі оператора ({ended_reason}). "
                        "Дзвінки припинено.",
                    )
            except Exception as e:  # noqa: BLE001 — CRM must not block the disposition
                log.warning("orchestrator.dead_number_crm_failed", error=str(e))
            return
        if (ended_reason or "").strip():
            # A call the carrier placed — whatever the candidate did with it.
            line_health.record_success()
        # An empty call (no transcript — dropped, busy, 0s) must never overwrite a real
        # conversation this candidate already had. Vapi/reconcile can finalize a dead
        # attempt AFTER the successful one, which used to blank the CRM card ("Немає
        # транскрипту для аналізу"), push the candidate back to Недозвін and fire a
        # "не змогли додзвонитися" Telegram message at someone Eva had already screened.
        if not (transcript or "").strip() and candidate is not None:
            async with session_scope() as sess:
                spoken = (await sess.execute(
                    select(Call.id).where(
                        Call.candidate_id == candidate.id,
                        Call.id != db_call.id,
                        Call.transcript.isnot(None),
                        Call.transcript != "",
                    ).limit(1)
                )).scalar_one_or_none()
            if spoken is not None:
                db_call.ended_at = datetime.utcnow()
                db_call.duration_sec = int(duration_sec)
                db_call.status = CallStatus.FAILED
                log.info(
                    "orchestrator.empty_call_ignored",
                    candidate_id=candidate.id, call_id=db_call.id, real_call_id=spoken,
                )
                return

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
        db_call.audio_url = (recording_url or "")[:512] or None
        db_call.transcript = transcript
        db_call.ai_summary = summary.summary
        # Clamp to the column widths — a model can return anything.
        db_call.sentiment = (summary.sentiment or "")[:16] or None
        db_call.objections = summary.objections
        db_call.language_used = (summary.language or "")[:8] or None
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
                summary.reject_reason = "not_target"  # off-portrait age → Не ЦА

        # Never pass a candidate to the recruiter without the basics. The model
        # sometimes marks qualified on partial data (experience only, no age/region).
        if summary.qualified and (not summary.candidate_age or not (summary.candidate_region or (candidate.region if candidate else None))):
            log.info(
                "orchestrator.incomplete_qualify",
                age=summary.candidate_age,
                region=summary.candidate_region,
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
                            "source": _sources.written(candidate.source),
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

        needs_tg_outreach: str | None = None
        reason = getattr(summary, "reject_reason", "none")
        # How much phone budget this person has already consumed — drives both the
        # "budget exhausted" disposition below and the activity block on the card.
        calls_total, talked_sec = 0, 0
        if candidate:
            async with session_scope() as sess:
                row = (await sess.execute(
                    select(func.count(Call.id), func.max(Call.duration_sec))
                    .where(Call.candidate_id == candidate.id)
                )).one()
                calls_total = int(row[0] or 0)
                talked_sec = int(row[1] or 0)
        if candidate:
            if summary.qualified:
                # Talked and fits → handed to the recruiter → В роботі.
                candidate.status = CandidateStatus.MANAGER_REVIEW
                new_stage = STAGE_MAP.get("manager_review")   # 3 В роботі
            elif (
                getattr(summary, "potentially_fit", False)
                and summary.spoke_with_candidate
            ):
                # Talked, looks promising, but region/age not finished. Do not redial —
                # finish it in Telegram. Stays in the Відібрано pool meanwhile.
                candidate.status = CandidateStatus.CALL_DONE
                new_stage = STAGE_MAP.get("call_done")         # 2 Відібрано
                needs_tg_outreach = "collect_info"
            elif getattr(summary, "time_waster", False) or reason == "misbehaved":
                # Rude / trolling / bad conduct → Не підходить нам.
                candidate.status = CandidateStatus.CLOSED
                new_stage = STAGE_MAP.get("we_rejected")        # 33 Не підходить
            elif summary.spoke_with_candidate and reason == "not_target":
                # Off-portrait from the start (region / age / no relevant field) → Не ЦА.
                candidate.status = CandidateStatus.CLOSED
                new_stage = STAGE_MAP.get("not_target")         # 34 Не ЦА
            elif summary.spoke_with_candidate and duration_sec >= 60:
                # Screening happened and the candidate did not fit for a non-quality
                # reason (found a job, not interested, won't go full-time) → Не актуально.
                # Re-dialing someone who already answered the questions annoys people.
                candidate.status = CandidateStatus.CLOSED
                new_stage = STAGE_MAP.get("not_actual")         # 32 Не актуально
            elif _wants_callback(summary):
                # "Передзвоніть пізніше" / "я вам передзвоню" — schedule instead of
                # letting the promise evaporate. Stays in the pool; the dispatcher picks
                # it up once callback_at passes, and the next call resumes the
                # transcript rather than starting the script over.
                candidate.status = CandidateStatus.IN_CALL_QUEUE
                candidate.callback_at = _callback_moment(
                    summary, ZoneInfo(self._settings.app_timezone)
                )
                new_stage = STAGE_MAP.get("call_done")          # 2 Відібрано (пул)
                log.info(
                    "orchestrator.callback_scheduled",
                    candidate_id=candidate.id, at=str(candidate.callback_at),
                )
            elif calls_total >= HARD_CALL_CAP:
                # Call budget fully spent and we never got a real conversation. Sitting
                # in Недозвін forever is exactly what recruiters complained about —
                # dispose of it explicitly so the funnel reflects reality.
                candidate.status = CandidateStatus.CLOSED
                new_stage = STAGE_MAP.get("not_actual")        # 32 Не актуально
                log.info(
                    "orchestrator.call_budget_exhausted",
                    candidate_id=candidate.id, calls=calls_total, cap=HARD_CALL_CAP,
                )
            elif candidate.call_attempts >= self._settings.call_max_attempts:
                candidate.status = CandidateStatus.UNREACHABLE
                new_stage = STAGE_MAP.get("unreachable")
                # Phone did not work out — try Telegram instead. A plain
                # no-answer gets the work number too; a broken line does not
                # (calling again would fail the same way).
                needs_tg_outreach = (
                    "bad_connection" if summary.connection_problem else "no_answer"
                )
            else:
                candidate.status = CandidateStatus.IN_CALL_QUEUE
                # Do NOT touch the CRM stage for someone we have not actually
                # spoken to: recruiters park old leads deliberately, and moving
                # them makes 3-week-old applications look brand new. The calling
                # queue lives in our DB, not in the CRM stage.
                new_stage = STAGE_MAP.get("in_call_queue") if summary.spoke_with_candidate else None

        # Unreachable by phone -> reach out in Telegram instead. Capture whether the
        # message actually went out — many UA numbers are not on Telegram or hide their
        # phone, so the note must not claim "written in Telegram" when nothing was sent.
        tg_sent = False
        tg_error: str | None = None
        if needs_tg_outreach and candidate and candidate.phone_e164:
            try:
                async with httpx.AsyncClient(timeout=20) as tg:
                    r = await tg.post(
                        f"{self._settings.tguserbot_url}/send_outreach",
                        json={
                            "phone": candidate.phone_e164,
                            "name": candidate.full_name or "",
                            "kind": needs_tg_outreach,
                        },
                    )
                    try:
                        body = r.json() or {}
                        tg_sent = bool(body.get("ok"))
                        if not tg_sent:
                            tg_error = str(body.get("error") or "")[:80]
                    except Exception:
                        tg_sent = False
                    log.info(
                        "orchestrator.tg_outreach",
                        kind=needs_tg_outreach,
                        status=r.status_code,
                        sent=tg_sent,
                        resp=r.text[:200],
                        candidate_id=candidate.id,
                    )
            except Exception as e:
                log.warning("orchestrator.tg_outreach_failed", error=str(e))


        # Deferred-KeyCRM mode: create the lead now that we have a
        # verdict, then set the stage to match. Skip creation on clear
        # non-signals (no answer + retry left) — that just retries later.
        if not candidate.keycrm_lead_id:
            should_push = (
                summary.qualified
                or (summary.spoke_with_candidate and duration_sec >= 60)
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
                    age=summary.candidate_age,
                    city=summary.candidate_region,
                    resume_link=(
                        f"{self._settings.app_base_url.rstrip('/')}/resume/{candidate.id}"
                        if self._settings.resume_link_mode == "selfhosted" and candidate.resume_text
                        else candidate.work_ua_url
                    ),
                )
                await self._keycrm.append_manager_comment(
                    candidate.keycrm_lead_id,
                    _activity_note(
                        calls_total=calls_total,
                        talked_sec=talked_sec,
                        tg_sent=tg_sent,
                        tg_error=tg_error,
                        label=_outcome_label(candidate.status, summary, tg_sent),
                        last_reason=ended_reason,
                    )
                    + "\n\n"
                    + f"джерело: {_sources.label(candidate.source)} | "
                    f"дзвінок {datetime.now(ZoneInfo(self._settings.app_timezone)):%d.%m %H:%M} | "
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

        # Save the person as a buyer (green 'client' check), link the card to it —
        # this also covers imported cards that create_lead never touched — and put
        # 'called? / result' on that buyer's note so a recruiter tells at a glance
        # whether Eva already worked this person. Buyers dedupe by phone.
        if candidate.phone_e164:
            try:
                buyer_id = await self._keycrm.ensure_buyer(
                    full_name=candidate.full_name,
                    phone=candidate.phone_e164,
                    email=candidate.email,
                )
                if buyer_id:
                    if candidate.keycrm_lead_id:
                        await self._keycrm.link_card_to_buyer(
                            candidate.keycrm_lead_id, buyer_id
                        )
                    label = _outcome_label(candidate.status, summary, tg_sent)
                    await self._keycrm.write_buyer_call_status(
                        buyer_id,
                        f"{datetime.now(ZoneInfo(self._settings.app_timezone)):%d.%m %H:%M} · "
                        f"{label} · дзвінків {calls_total}/{HARD_CALL_CAP}"
                        + ("" if tg_sent else f" · TG ❌ {tg_error or 'недоступний'}"),
                    )
            except Exception as e:
                log.warning("orchestrator.buyer_status_failed", error=str(e))
