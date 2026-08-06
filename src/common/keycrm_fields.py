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
# Real status ids of KeyCRM pipeline 1 ("1 Етап Менеджер з продажу").
# Previously all zeros — the API fill never ran, so no lead ever changed stage.
#
# Client funnel semantics (2026-07-22):
#   2  Відібрано      — matched the portrait, waiting for a call or being called
#   3  В роботі       — already talked/chatted AND selected for the recruiter/interview
#   31 Недозвін       — 2 calls + 1 message, no answer
#   32 Не актуально   — not interested / found a job / no answer after all attempts
#   33 Не підходить   — rude / bad behaviour / clearly unsuitable by conduct
#   34 Не ЦА          — off-portrait from the start (region / age / no relevant experience)
#   10 Запросили на 1 тур
STAGE_MAP: dict[str, int] = {
    "new_resume": 2,          # Відібрано — matched portrait, waiting for call
    "filtered": 2,            # Відібрано
    "in_call_queue": 2,       # Відібрано — in the calling pool
    "calling": 2,             # Відібрано — being called
    "call_done": 2,           # Відібрано — talked, still collecting / TG follow-up
    "manager_review": 3,      # В роботі — talked/qualified, handed to the recruiter
    "unreachable": 31,        # Недозвін — 2 calls + message, no answer
    "not_actual": 32,         # Не актуально — not interested / found job / no answer
    "we_rejected": 33,        # Не підходить нам — rude / bad behaviour
    "not_target": 34,         # Не ЦА — off-portrait (region/age/no relevant experience)
    "interview_scheduled": 10,  # Запросили на 1 тур
    # legacy alias: any leftover "closed" path defaults to Не актуально
    "closed": 32,
}


# Reverse view for the CRM→our sync. Which LIVE card stages mean "a recruiter now
# owns or has dispositioned this candidate → Eva must stop"? Stages 1 Новий / 2 Відібрано
# / 31 Недозвін are Eva's own working stages → she continues. Everything else stops her.
_CRM_STAGE_STOP: dict[int, str] = {
    3: "manager_review",       # В роботі — recruiter took over
    4: "manager_review",       # Дійшов на 1 тур
    30: "manager_review",      # Підтвердили участь
    10: "interview_scheduled",  # Запросили на 1 тур
    5: "closed",               # Не підтвердили участь
    32: "closed",              # Не актуально
    33: "closed",              # Не підходить нам
    34: "closed",              # Не ЦА
    82: "closed",              # Кадровий резерв
}


def crm_stage_stop_status(status_id: int | None) -> str | None:
    """Given a card's live KeyCRM status_id, return the local candidate status Eva
    should move to because a recruiter took over or dispositioned the card — or None if
    the stage is still one of Eva's own working stages (1 Новий / 2 Відібрано / 31 Недозвін)."""
    return _CRM_STAGE_STOP.get(status_id) if status_id is not None else None


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
