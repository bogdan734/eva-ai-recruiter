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
        if cand:
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
