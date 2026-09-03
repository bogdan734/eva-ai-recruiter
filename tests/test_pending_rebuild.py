"""A parked apply must come back complete enough to act on.

robota.ua hides most phones, so those applies are parked and re-examined later.
Rebuilding one dropped `resumeId` — and opening a contact starts with

    resume_id = int(apply.get("resumeId") or 0)
    if not resume_id:
        return False

so every parked apply was refused before any rule was consulted. The queue grew
to 181 and could not have drained a single entry, which is why a recruiter found
twelve unviewed responses on one posting that never reached the CRM.

The queue counter said 181 and the openings counter said 0, and neither was an
error. That is the shape this project keeps rediscovering.
"""
import pytest

from src.integrations.robotaua_sync import _pending_as_apply

ENTRY = {
    "resume_id": 25916731,
    "name": "Деменко Ольга",
    "vacancy_id": 11249166,
    "resume_type": "Interaction",
    "city_id": 4,
    "speciality": "Оператор 1с та бухгалтер",
    "file_name": "",
    "file_path": "",
    "resume_file": None,
    "first_seen": "2026-08-24T08:52:11",
    "last_probe": "2026-09-02T13:07:04",
}


class TestOpeningIsPossible:
    def test_resume_id_survives_the_rebuild(self):
        """Without this the contact can never be opened, whatever the rules say."""
        assert _pending_as_apply("123", ENTRY).get("resumeId") == 25916731

    def test_the_opening_guard_would_pass(self):
        apply = _pending_as_apply("123", ENTRY)
        assert int(apply.get("resumeId") or 0) > 0

    def test_missing_resume_id_stays_falsy_rather_than_crashing(self):
        """`AttachedFile` applies genuinely have none — that must not raise."""
        entry = dict(ENTRY, resume_id=0)
        assert not _pending_as_apply("123", entry).get("resumeId")


class TestRoutingFacts:
    @pytest.mark.parametrize(
        "key,expected",
        [
            ("id", 123),
            ("vacancyId", 11249166),
            ("cityId", 4),
            ("resumeType", "Interaction"),
            ("name", "Деменко Ольга"),
        ],
    )
    def test_carried_through(self, key, expected):
        assert _pending_as_apply("123", ENTRY).get(key) == expected

    def test_speciality_reaches_scoring(self):
        """fit_score reads it; losing it would score every parked apply blind."""
        assert "бухгалтер" in (_pending_as_apply("123", ENTRY).get("speciality") or "").lower()


class TestAttachedFileRoute:
    def test_file_references_survive(self):
        entry = dict(ENTRY, file_name="cv.pdf", file_path="/x/cv.pdf", resume_file="cv.pdf")
        apply = _pending_as_apply("9", entry)
        assert apply.get("fileName") == "cv.pdf"
        assert apply.get("filePath") == "/x/cv.pdf"
        assert apply.get("resumeFile") == "cv.pdf"
