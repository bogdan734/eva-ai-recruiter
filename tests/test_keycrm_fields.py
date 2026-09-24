from datetime import UTC, datetime

from src.common import keycrm_fields
from src.common.keycrm_fields import (
    FIELD_MAP,
    STAGE_MAP,
    build_lead_payload,
    crm_stage_stop_status,
    load_mapping_from_api,
    load_stages_from_api,
)
from src.common.models import CandidateStatus


def test_field_map_has_all_24_keys():
    expected_keys = {
        "work_ua_url",
        "region",
        "desired_position",
        "experience_years",
        "languages",
        "match_score",
        "vacancy_id",
        "call_attempts",
        "last_call_at",
        "last_call_status",
        "last_call_duration_sec",
        "audio_url",
        "transcript",
        "ai_summary",
        "sentiment",
        "objections_raised",
        "language_used",
        "tokens_input",
        "tokens_output",
        "cost_usd",
        "tags",
        "manager_assigned",
        "interview_scheduled_at",
        "source",
    }
    assert set(FIELD_MAP.keys()) == expected_keys


def test_stage_map_covers_every_status_we_write():
    """Every local status must map to a CRM stage -- a missing key means cards
    silently stop moving. Grew from 9 to 12 when the closing reasons were split
    (не актуально / не підходить нам / не ЦА) instead of one generic «closed»."""
    assert set(STAGE_MAP.keys()) == {
        "new_resume",
        "filtered",
        "in_call_queue",
        "calling",
        "call_done",
        "manager_review",
        "unreachable",
        "not_actual",
        "we_rejected",
        "not_target",
        "interview_scheduled",
        "closed",
    }


def test_in_work_does_not_stop_a_screening_eva_finishes_in_telegram():
    """Since 02.09 Eva parks her own unfinished screenings in «В роботі» (3) and
    writes to them in Telegram. The sync used to read that back as a recruiter
    taking over, and the Telegram gate then kept her silent when they replied."""
    assert crm_stage_stop_status(3, "call_done") is None
    assert crm_stage_stop_status(3, CandidateStatus.CALL_DONE) is None


def test_in_work_does_not_stop_a_promised_callback():
    due = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
    assert crm_stage_stop_status(3, "in_call_queue", due) is None


def test_in_work_still_stops_a_candidate_eva_had_in_the_pool():
    assert crm_stage_stop_status(3) == "manager_review"
    assert crm_stage_stop_status(3, "in_call_queue") == "manager_review"
    assert crm_stage_stop_status(3, "unreachable") == "manager_review"


def test_recruiter_disposition_still_stops_a_telegram_follow_up():
    assert crm_stage_stop_status(32, "call_done") == "closed"
    assert crm_stage_stop_status(10, "call_done") == "interview_scheduled"
    assert crm_stage_stop_status(34, "in_call_queue", datetime.now(UTC)) == "closed"


def test_eva_working_stages_never_stop():
    for stage in (1, 2, 31, None):
        assert crm_stage_stop_status(stage, "call_done") is None


def test_load_mapping_populates_ids():
    raw = [
        {"id": 101, "name": "URL", "code": "work_ua_url"},
        {"id": 102, "name": "Регіон", "code": "region"},
        {"id": 103, "name": "Unknown", "code": "not_in_map"},
    ]
    # Save and restore to avoid bleed
    original = dict(keycrm_fields.FIELD_MAP)
    try:
        load_mapping_from_api(funnel_id=1, raw_fields=raw)
        assert keycrm_fields.FIELD_MAP["work_ua_url"] == 101
        assert keycrm_fields.FIELD_MAP["region"] == 102
        assert "not_in_map" not in keycrm_fields.FIELD_MAP
    finally:
        keycrm_fields.FIELD_MAP.clear()
        keycrm_fields.FIELD_MAP.update(original)


def test_load_stages_populates():
    original = dict(keycrm_fields.STAGE_MAP)
    try:
        load_stages_from_api(
            [
                {"id": 1, "code": "new_resume"},
                {"id": 2, "code": "filtered"},
            ]
        )
        assert keycrm_fields.STAGE_MAP["new_resume"] == 1
        assert keycrm_fields.STAGE_MAP["filtered"] == 2
    finally:
        keycrm_fields.STAGE_MAP.clear()
        keycrm_fields.STAGE_MAP.update(original)


def test_build_lead_payload_minimal():
    original = dict(keycrm_fields.FIELD_MAP)
    try:
        # No field ids → no custom_fields in payload
        body = build_lead_payload(
            name="Іван Петренко",
            phone="+380671234567",
            email="ivan@example.com",
            custom={"region": "Київська", "match_score": 75},
        )
        assert body["title"] == "Іван Петренко"
        assert body["contact"]["phone"] == "+380671234567"
        assert body["contact"]["email"] == "ivan@example.com"
        assert "custom_fields" not in body  # all FIELD_MAP entries are 0
    finally:
        keycrm_fields.FIELD_MAP.clear()
        keycrm_fields.FIELD_MAP.update(original)


def test_build_lead_payload_with_mapping():
    original = dict(keycrm_fields.FIELD_MAP)
    try:
        keycrm_fields.FIELD_MAP["region"] = 102
        keycrm_fields.FIELD_MAP["match_score"] = 106
        body = build_lead_payload(
            name="Іван",
            phone="+380671234567",
            email=None,
            custom={"region": "Київська", "match_score": 75, "unknown_key": "x"},
            stage_id=5,
        )
        assert body["stage_id"] == 5
        cf = body["custom_fields"]
        ids = {f["id"] for f in cf}
        assert ids == {102, 106}
    finally:
        keycrm_fields.FIELD_MAP.clear()
        keycrm_fields.FIELD_MAP.update(original)
