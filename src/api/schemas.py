from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class HealthResponse(BaseModel):
    status: str
    env: str


class KeyCRMLead(BaseModel):
    id: int
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    stage_id: int | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class KeyCRMWebhookPayload(BaseModel):
    event: str
    lead: KeyCRMLead
    timestamp: datetime | None = None


class VapiWebhookPayload(BaseModel):
    type: Literal[
        "end-of-call-report",
        "status-update",
        "hang",
        "function-call",
        "transcript",
        "tool-calls",
    ]
    call_id: str | None = None
    assistant_id: str | None = None
    customer_phone: str | None = None
    direction: Literal["inbound", "outbound", "unknown"] = "unknown"
    transcript: str | None = None
    recording_url: str | None = None
    duration_sec: float | None = None
    cost: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten_vapi_envelope(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        msg = data.get("message") if isinstance(data.get("message"), dict) else None
        src = msg or data
        call = src.get("call") if isinstance(src.get("call"), dict) else {}
        customer = src.get("customer") if isinstance(src.get("customer"), dict) else {}
        if not customer and isinstance(call.get("customer"), dict):
            customer = call["customer"]
        assistant = src.get("assistant") if isinstance(src.get("assistant"), dict) else {}
        call_type = call.get("type") or src.get("call_type") or ""
        direction = "unknown"
        if "inbound" in str(call_type).lower():
            direction = "inbound"
        elif "outbound" in str(call_type).lower():
            direction = "outbound"
        out: dict[str, Any] = {
            "type": src.get("type") or data.get("type"),
            "call_id": data.get("call_id") or call.get("id") or src.get("call_id"),
            "assistant_id": data.get("assistant_id")
            or assistant.get("id")
            or src.get("assistant_id"),
            "customer_phone": data.get("customer_phone")
            or customer.get("number")
            or src.get("customer_phone"),
            "direction": data.get("direction") or direction,
            "transcript": data.get("transcript") or src.get("transcript"),
            "recording_url": data.get("recording_url")
            or src.get("recordingUrl")
            or src.get("recording_url"),
            "duration_sec": data.get("duration_sec")
            or src.get("durationSeconds")
            or src.get("duration_sec"),
            "cost": data.get("cost") or src.get("cost"),
            "analysis": data.get("analysis") or src.get("analysis"),
            "raw": data,
        }
        return {k: v for k, v in out.items() if v is not None or k in {"type", "raw", "direction"}}


class WorkUaInboundPayload(BaseModel):
    full_name: str
    phone: str
    email: str | None = None
    region: str | None = None
    desired_position: str | None = None
    work_ua_url: str | None = None
    vacancy_external_id: str | None = None
    source: str = "workua_response"
