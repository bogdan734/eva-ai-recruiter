from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import structlog
from fastapi import FastAPI, Header, HTTPException, Request

from src.common.settings import get_settings
from src.cost import usage

from .schemas import (
    HealthResponse,
    KeyCRMWebhookPayload,
    VapiWebhookPayload,
    TgOutcomePayload,
    TgProgressPayload,
    TokenUsagePayload,
    WorkUaInboundPayload,
)
from .services import (
    handle_assistant_request,
    handle_keycrm_event,
    handle_tg_outcome,
    handle_tg_progress,
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
    # Inbound: Vapi asks who should answer BEFORE connecting — reply synchronously
    # with the assistant (Eva by default, a brief handoff line for handed-off callers).
    if payload.type == "assistant-request":
        return await handle_assistant_request(payload)
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


@app.get("/recordings/{vapi_call_id}")
async def get_recording(vapi_call_id: str):
    """Redirect to a freshly signed recording URL.

    Vapi's stored recordingUrl is not publicly fetchable and its presigned URLs
    expire within the hour, so the CRM card links here instead.
    """
    import httpx
    from fastapi.responses import RedirectResponse

    s = get_settings()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"https://api.vapi.ai/call/{vapi_call_id}",
            headers={"Authorization": f"Bearer {s.vapi_api_key}"},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=404, detail="call not found")
        art = (r.json() or {}).get("artifact") or {}

    url = art.get("presignedMonoUrl") or art.get("presignedStereoUrl")
    if not url:
        raise HTTPException(status_code=404, detail="recording not available")
    return RedirectResponse(url)


@app.get("/resume/{candidate_id}")
async def get_resume(candidate_id: int):
    """Self-hosted resume page (variant 2). Renders the resume text we stored at
    ingest — a stable link that works without a work.ua login, and uniform across
    boards (work.ua, robota.ua). Selected via RESUME_LINK_MODE=selfhosted."""
    from html import escape
    from fastapi.responses import HTMLResponse
    from src.common.db import session_scope
    from src.common.models import Candidate

    async with session_scope() as session:
        cand = await session.get(Candidate, candidate_id)
    if not cand or not cand.resume_text:
        raise HTTPException(status_code=404, detail="resume not available")
    name = escape(cand.full_name or "Кандидат")
    body = escape(cand.resume_text).replace("\n", "<br>")
    extra = " · ".join(filter(None, [escape(cand.region or ""), escape(cand.phone_e164 or "")]))
    page = (
        "<!doctype html><html lang=\"uk\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>Резюме — {name}</title><style>"
        "body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:760px;"
        "margin:24px auto;padding:0 16px;line-height:1.55;color:#1a1a1a}"
        "h1{font-size:20px;margin:0 0 4px}.meta{color:#777;font-size:13px;margin-bottom:16px}"
        ".card{background:#fafafa;border:1px solid #ececec;border-radius:10px;padding:20px;font-size:14px}"
        "</style></head><body>"
        f"<h1>{name}</h1><div class=\"meta\">{extra}</div><div class=\"card\">{body}</div>"
        "</body></html>"
    )
    return HTMLResponse(page)


@app.post("/internal/tg-outcome")
async def tg_outcome(
    payload: TgOutcomePayload,
    x_internal_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Called by the Telegram userbot when a chat reaches a verdict."""
    s = get_settings()
    if x_internal_token != s.internal_api_token:
        raise HTTPException(status_code=401, detail="bad internal token")
    return await handle_tg_outcome(
        peer_id=payload.peer_id,
        name=payload.name,
        username=payload.username,
        phone=payload.phone,
        verdict=payload.verdict,
        region=payload.region,
        age=payload.age,
        summary=payload.summary,
        transcript=payload.transcript,
        reason=payload.reason,
    )


# Statuses where a recruiter now owns the candidate — Eva must stop engaging.
_HANDOFF_STATUSES = {"manager_review", "interview_scheduled", "closed"}


@app.get("/internal/tg-gate")
async def tg_gate(
    peer: str,
    phone: str | None = None,
    x_internal_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Should Eva keep talking to this Telegram peer? Returns engage=False once the
    candidate has been handed to a recruiter (manager_review / interview / closed),
    so Eva goes silent instead of re-opening a dialog the recruiter now owns. The
    candidate is resolved exactly like tg-outcome: real phone if known, else the
    stable tg<peer> surrogate."""
    s = get_settings()
    if x_internal_token != s.internal_api_token:
        raise HTTPException(status_code=401, detail="bad internal token")
    from sqlalchemy import select
    from src.common.db import session_scope
    from src.common.models import Candidate
    from src.common.phone import normalize_phone

    keys = [f"tg{peer}"[:20]]
    if phone:
        try:
            norm = normalize_phone(phone)
            if norm:
                keys.insert(0, norm)
        except Exception:
            pass
    async with session_scope() as session:
        cand = (await session.execute(
            select(Candidate).where(Candidate.phone_e164.in_(keys))
        )).scalars().first()
    status = cand.status if cand else None
    return {"engage": status not in _HANDOFF_STATUSES, "status": status, "found": cand is not None}


@app.post("/internal/tg-progress")
async def tg_progress(
    payload: TgProgressPayload,
    x_internal_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Live Telegram transcript update — keeps a candidate's CRM card current before a
    verdict (no-op if they have no card yet)."""
    s = get_settings()
    if x_internal_token != s.internal_api_token:
        raise HTTPException(status_code=401, detail="bad internal token")
    return await handle_tg_progress(
        peer_id=payload.peer_id,
        name=payload.name,
        username=payload.username,
        phone=payload.phone,
        transcript=payload.transcript,
    )


@app.post("/internal/token-usage")
async def token_usage(
    payload: TokenUsagePayload,
    x_internal_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Anthropic usage from a service without its own Postgres — the userbot.

    Fire-and-forget by design: the caller must not fail a candidate reply because
    accounting is down, so this always answers 200 once the token checks out.
    """
    s = get_settings()
    if x_internal_token != s.internal_api_token:
        raise HTTPException(status_code=401, detail="bad internal token")
    await usage.record(
        payload.component or usage.TG_USERBOT,
        None,
        model=payload.model,
        tokens_in=payload.tokens_input,
        tokens_out=payload.tokens_output,
    )
    return {"ok": True}
