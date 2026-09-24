"""response_date visibility in manager_comment -- added 2026-09-03 after the
recruiter (Svitlana) reported 203 backfilled cards with no way to tell a
same-day applicant from a two-month-old one. KeyCRM has no date-typed custom
field and won't let the API create one (405), so the response date has to be
the first thing in manager_comment instead.
"""
from datetime import datetime

from src.api.inbound_router import IngestPayload, _format_manager_comment


def test_response_date_is_first_and_visible():
    payload = IngestPayload(
        full_name="Тест Тестович",
        phone_raw="+380991112233",
        response_date=datetime(2026, 7, 1, 9, 12),
    )
    comment = _format_manager_comment(payload, region=None)
    assert comment.startswith("📅 дата відгуку: 01.07.2026 09:12")


def test_no_response_date_omits_the_line():
    payload = IngestPayload(full_name="Тест Тестович", phone_raw="+380991112233")
    comment = _format_manager_comment(payload, region=None)
    assert "дата відгуку" not in comment


def test_response_date_precedes_other_bits():
    payload = IngestPayload(
        full_name="Тест Тестович",
        phone_raw="+380991112233",
        response_date=datetime(2026, 9, 1, 10, 0),
        experience_years=3,
        source="workua_response_send",
    )
    comment = _format_manager_comment(payload, region="Київ")
    date_pos = comment.index("дата відгуку")
    region_pos = comment.index("Київ")
    assert date_pos < region_pos
