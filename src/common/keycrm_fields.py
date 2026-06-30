"""KeyCRM custom-field name ↔ id mapping.

KeyCRM custom fields are addressed by numeric IDs, but our code talks in tech_keys.
At deploy time we fetch the funnel field-list once and populate this map.
For now, all IDs are placeholders — fill them in after creating fields in KeyCRM UI.
"""
from __future__ import annotations

from typing import Any

# tech_key -> KeyCRM field id. Filled at startup from KeyCRM API.
FIELD_MAP: dict[str, int] = {
    "work_ua_url": 0,
    "region": 0,
    "desired_position": 0,
    "experience_years": 0,
    "languages": 0,
    "match_score": 0,
    "vacancy_id": 0,
    "call_attempts": 0,
    "last_call_at": 0,
    "last_call_status": 0,
    "last_call_duration_sec": 0,
    "audio_url": 0,
    "transcript": 0,
    "ai_summary": 0,
    "sentiment": 0,
    "objections_raised": 0,
    "language_used": 0,
    "tokens_input": 0,
    "tokens_output": 0,
    "cost_usd": 0,
    "tags": 0,
    "manager_assigned": 0,
    "interview_scheduled_at": 0,
    "source": 0,
}

# Stage tech_key -> KeyCRM stage id
STAGE_MAP: dict[str, int] = {
    "new_resume": 0,
    "filtered": 0,
    "in_call_queue": 0,
    "calling": 0,
    "unreachable": 0,
    "call_done": 0,
    "manager_review": 0,
    "interview_scheduled": 0,
    "closed": 0,
}


def build_lead_payload(
    *,
    name: str,
    phone: str,
    email: str | None,
    custom: dict[str, Any],
    stage_id: int | None = None,
) -> dict[str, Any]:
    """Translate our domain fields into KeyCRM lead payload shape."""
    body: dict[str, Any] = {
        "title": name,
        "contact": {"full_name": name, "phone": phone},
    }
    if email:
        body["contact"]["email"] = email
    if stage_id:
        body["stage_id"] = stage_id

    fields_payload = []
    for tech_key, value in custom.items():
        fid = FIELD_MAP.get(tech_key, 0)
        if fid and value is not None:
            fields_payload.append({"id": fid, "value": value})
    if fields_payload:
        body["custom_fields"] = fields_payload
    return body


def load_mapping_from_api(funnel_id: int, raw_fields: list[dict[str, Any]]) -> None:
    """Populate FIELD_MAP from KeyCRM /custom-fields response.

    Expected shape: [{"id": 123, "name": "URL резюме work.ua", "code": "work_ua_url"}, ...]
    The `code` slot should be set in KeyCRM UI when creating the field.
    """
    for f in raw_fields:
        code = f.get("code") or ""
        if code in FIELD_MAP:
            FIELD_MAP[code] = int(f["id"])


def load_stages_from_api(raw_stages: list[dict[str, Any]]) -> None:
    """Populate STAGE_MAP from KeyCRM /pipelines/{id}/stages response.

    Match by stage `code` (we set this when creating stages).
    """
    for s in raw_stages:
        code = s.get("code") or ""
        if code in STAGE_MAP:
            STAGE_MAP[code] = int(s["id"])
