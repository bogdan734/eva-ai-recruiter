from __future__ import annotations

import structlog

from src.api.inbound_router import IngestPayload, InboundRouter
from src.call.orchestrator import CallOrchestrator

from .schemas import KeyCRMWebhookPayload, VapiWebhookPayload, WorkUaInboundPayload

log = structlog.get_logger()


async def handle_keycrm_event(event: str, payload: KeyCRMWebhookPayload) -> None:
    log.info("keycrm.handle", event=event, lead_id=payload.lead.id, stage=payload.lead.stage_id)


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
        )
        return
    await orchestrator.process_end_of_call(
        vapi_call_id=payload.call_id,
        transcript=payload.transcript,
        duration_sec=payload.duration_sec or 0.0,
        recording_url=payload.recording_url,
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
