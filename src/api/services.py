from __future__ import annotations

import structlog

from typing import Any

from src.api.inbound_router import IngestPayload, InboundRouter
from src.call.orchestrator import CallOrchestrator
from src.common.settings import get_settings

from .schemas import KeyCRMWebhookPayload, VapiWebhookPayload, WorkUaInboundPayload

log = structlog.get_logger()


async def handle_keycrm_event(event: str, payload: KeyCRMWebhookPayload) -> None:
    """A card changed in KeyCRM (only fires if the client configures KeyCRM automations
    to POST here). Mirror a recruiter's stage move into our DB so Eva stops working a
    candidate they took over. The poll job sync_crm_stages is the fallback when no
    webhook is set up — this just makes it real-time."""
    from sqlalchemy import select

    from src.common.db import session_scope
    from src.common.keycrm_fields import crm_stage_stop_status
    from src.common.models import Candidate, CandidateStatus

    stop = crm_stage_stop_status(payload.lead.stage_id)
    log.info("keycrm.handle", event=event, lead_id=payload.lead.id,
             stage=payload.lead.stage_id, stop=stop)
    if not stop:
        return
    async with session_scope() as session:
        cand = (await session.execute(
            select(Candidate).where(Candidate.keycrm_lead_id == payload.lead.id)
        )).scalars().first()
        # The card may sit in «В роботі» because Eva put it there herself.
        if cand and crm_stage_stop_status(
            payload.lead.stage_id, cand.status, cand.callback_at
        ):
            target = CandidateStatus(stop)
            if cand.status != target:
                cand.status = target
                log.info("keycrm.webhook_stop candidate=%d lead=%d -> %s",
                         cand.id, payload.lead.id, stop)


# Statuses where a recruiter now owns the candidate — Eva must not re-screen them.
_HANDOFF_STATUSES = {"manager_review", "interview_scheduled", "closed"}

# Spoken to a handed-off candidate who calls in. Ends with "Гарного дня!" so Eva's
# existing endCallPhrases hang up right after the line (maxDurationSeconds is a backstop).
_HANDOFF_INBOUND_LINE = (
    "Доброго дня! Дякую за дзвінок. Вашу заявку вже передано рекрутеру — "
    "він найближчим часом звʼяжеться з вами, щоб узгодити деталі. Гарного дня!"
)


async def handle_assistant_request(payload: VapiWebhookPayload) -> dict[str, Any]:
    """Vapi asks which assistant answers an inbound call. Default: Eva. But if the
    caller is a candidate already handed to a recruiter (manager_review / interview /
    closed), Eva just says the recruiter has it and ends — no re-screening. Fail-safe:
    unknown caller or any lookup error → Eva answers normally, so inbound never breaks."""
    s = get_settings()
    eva = s.vapi_assistant_id
    caller = payload.customer_phone or ""
    try:
        from sqlalchemy import select

        from src.common.db import session_scope
        from src.common.models import Candidate
        from src.common.phone import normalize_phone

        try:
            norm = normalize_phone(caller)
        except Exception:
            norm = None
        status = None
        if norm:
            async with session_scope() as session:
                cand = (await session.execute(
                    select(Candidate).where(Candidate.phone_e164 == norm)
                )).scalars().first()
                status = cand.status if cand else None
        if status in _HANDOFF_STATUSES:
            log.info("vapi.assistant_request.handoff_decline", caller=caller, status=status)
            return {
                "assistantId": eva,
                "assistantOverrides": {
                    "firstMessage": _HANDOFF_INBOUND_LINE,
                    "firstMessageMode": "assistant-speaks-first",
                    "maxDurationSeconds": 30,
                },
            }
    except Exception as e:
        log.warning("vapi.assistant_request.lookup_failed", error=str(e), caller=caller)
    log.info("vapi.assistant_request.eva", caller=caller)
    return {"assistantId": eva}


async def handle_vapi_event(payload: VapiWebhookPayload) -> None:
    if payload.type != "end-of-call-report":
        log.info("vapi.event", type=payload.type, call_id=payload.call_id)
        return
    if not payload.call_id or not payload.transcript:
        log.warning("vapi.end_of_call.missing_data", call_id=payload.call_id)
        return
    orchestrator = CallOrchestrator()
    if payload.direction == "inbound":
        await orchestrator.process_inbound_call(
            vapi_call_id=payload.call_id,
            caller_phone=payload.customer_phone or "",
            transcript=payload.transcript,
            duration_sec=payload.duration_sec or 0.0,
            recording_url=payload.recording_url,
            ended_reason=payload.ended_reason,
        )
        return
    await orchestrator.process_end_of_call(
        vapi_call_id=payload.call_id,
        transcript=payload.transcript,
        duration_sec=payload.duration_sec or 0.0,
        recording_url=payload.recording_url,
        ended_reason=payload.ended_reason,
    )


# Question wording changes; what the question is *for* does not. Each field is
# found by keyword over the question text, in both Ukrainian and Russian, so
# rewording a question or inserting a new one cannot silently drop a column.
_FORM_FIELD_KEYWORDS: dict[str, tuple[str, ...]] = {
    # Stems, not whole words: «Ваше ПІБ», «На яку посаду претендуєте?» and «Ваша
    # посада» are all the same question in different clothes, and Ukrainian
    # declines the noun in most of them.
    "full_name": ("піб", "прізвищ", "ім'я", "імя", "фио", "фамили", "имя", "вас зовут", "вас звати"),
    "phone": ("телефон", "номер", "phone", "тел."),
    "email": ("пошт", "почт", "e-mail", "email", "мейл"),
    "region": ("міст", "город", "регіон", "регион", "област", "де ви", "где вы", "проживан"),
    "position": ("посад", "должност", "вакансі", "ваканси", "напрям", "направлен", "позиц"),
    "age": ("вік", "возраст", "скільки вам", "сколько вам", "років", "лет"),
}


def _pick_form_field(answers: dict[str, str], field: str) -> str | None:
    """First answer whose question mentions what we are looking for."""
    for question, value in answers.items():
        text = str(question or "").strip().lower()
        if not str(value or "").strip():
            continue
        if any(kw in text for kw in _FORM_FIELD_KEYWORDS[field]):
            return str(value).strip()
    return None


def _form_resume_text(answers: dict[str, str]) -> str:
    """Everything she asked and everything they answered, kept verbatim.

    The recruiter reads the card before deciding, and a form answer we did not
    map to a field is often the one that matters — a note about shift work, a
    licence category. Dropping it because it has no column would make the card
    worse than the spreadsheet row it came from.
    """
    return "\n".join(
        f"{str(q).strip()}: {str(v).strip()}"
        for q, v in answers.items()
        if str(v or "").strip()
    )


async def handle_google_form_submission(answers: dict[str, str]) -> dict:
    """One form submission -> one CRM card, filed under «Анкети».

    Treated as a self-applied candidate (is_response=True): the person came to
    us, so under the 04.09 policy they land in «На розгляді менеджера» and Єва
    never cold-calls them. That is the same handling the recruiter has been
    doing by hand for these rows.
    """
    from src.api.inbound_router import IngestPayload, InboundRouter

    name = _pick_form_field(answers, "full_name")
    phone = _pick_form_field(answers, "phone")
    if not name or not phone:
        # Worth a loud log rather than a silent 200: it means the form was
        # edited into a shape this mapping no longer recognises.
        log.warning(
            "googleform.unmapped_submission",
            questions=list(answers.keys())[:12],
            has_name=bool(name), has_phone=bool(phone),
        )
        return {"ok": False, "reason": "no name or phone in submission"}

    result = await InboundRouter().ingest(
        IngestPayload(
            full_name=name,
            phone_raw=phone,
            email=_pick_form_field(answers, "email"),
            region_raw=_pick_form_field(answers, "region"),
            desired_position=_pick_form_field(answers, "position"),
            resume_text=_form_resume_text(answers),
            source="googleform",
            is_response=True,
        )
    )
    log.info(
        "googleform.routed",
        accepted=result.accepted, duplicate=result.duplicate,
        candidate_id=result.candidate_id, lead_id=result.keycrm_lead_id,
        reason=result.reason,
    )
    return {
        "ok": result.accepted,
        "duplicate": result.duplicate,
        "candidate_id": result.candidate_id,
        "lead_id": result.keycrm_lead_id,
        "reason": result.reason,
    }


async def handle_workua_inbound(payload: WorkUaInboundPayload) -> None:
    router = InboundRouter()
    result = await router.ingest(
        IngestPayload(
            full_name=payload.full_name,
            phone_raw=payload.phone,
            email=payload.email,
            region_raw=payload.region,
            desired_position=payload.desired_position,
            work_ua_url=payload.work_ua_url,
            source=payload.source,
            # 2026-09-04 policy change: this endpoint carries a work.ua response
            # someone submitted manually/via recovery -- same is_response rule
            # as the automated poller applies here too.
            is_response=True,
        )
    )
    log.info(
        "workua.routed",
        accepted=result.accepted,
        duplicate=result.duplicate,
        candidate_id=result.candidate_id,
        reason=result.reason,
    )

from src.api.tg_outcome import handle_tg_outcome, handle_tg_progress  # noqa: E402,F401
