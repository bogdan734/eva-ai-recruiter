from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import structlog
from fastapi import FastAPI, Header, HTTPException, Request

from src.common.settings import get_settings

from .schemas import (
    HealthResponse,
    KeyCRMWebhookPayload,
    VapiWebhookPayload,
    WorkUaInboundPayload,
)
from .services import (
    handle_keycrm_event,
    handle_vapi_event,
    handle_workua_inbound,
)

logging.basicConfig(level=logging.INFO)
log = structlog.get_logger()

app = FastAPI(title="AI Recruiter API", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    s = get_settings()
    return HealthResponse(status="ok", env=s.app_env)


def _verify_hmac(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _verify_shared_secret(secret: str, provided: str | None) -> bool:
    if not provided or not secret:
        return False
    return hmac.compare_digest(secret, provided)


@app.post("/webhooks/keycrm/{event}")
async def keycrm_webhook(
    event: str,
    request: Request,
    x_keycrm_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    s = get_settings()
    body = await request.body()
    if s.app_env == "prod" and not _verify_hmac(s.keycrm_webhook_secret, body, x_keycrm_signature):
        raise HTTPException(401, "bad signature")
    payload = KeyCRMWebhookPayload.model_validate_json(body)
    log.info("keycrm.webhook", event=event, lead_id=payload.lead.id)
    await handle_keycrm_event(event, payload)
    return {"ok": True}


@app.post("/webhooks/vapi/events")
async def vapi_webhook(
    request: Request,
    x_vapi_signature: str | None = Header(default=None),
    x_vapi_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    s = get_settings()
    body = await request.body()
    if s.app_env == "prod":
        ok = _verify_hmac(s.vapi_webhook_secret, body, x_vapi_signature) or _verify_shared_secret(
            s.vapi_webhook_secret, x_vapi_secret
        )
        if not ok:
            raise HTTPException(401, "bad signature")
    payload = VapiWebhookPayload.model_validate_json(body)
    log.info("vapi.event", type=payload.type, call_id=payload.call_id)
    await handle_vapi_event(payload)
    return {"ok": True}


@app.post("/webhooks/workua/manual")
async def workua_manual(payload: WorkUaInboundPayload) -> dict[str, Any]:
    """Debug endpoint to inject a candidate manually (e.g. for testing the pipeline
    without waiting for a real work.ua response). Production flow is the cron
    poller in src/integrations/workua_sync."""
    log.info("workua.manual_inbound", phone=payload.phone)
    await handle_workua_inbound(payload)
    return {"ok": True}
