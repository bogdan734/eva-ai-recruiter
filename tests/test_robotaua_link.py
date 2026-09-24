"""parse_apply()'s resume_url (src/integrations/robotaua_api.py).

07.09.2026: the link shown on a CRM card was the bare public candidate/resume
page (`/candidates/{resumeId}`). The user wants the employer-cabinet "review
this application" page instead (`/my/vacancies/all/applies?id={applyId}-prof`)
-- what a recruiter actually lands on clicking the same apply from robota.ua's
own UI. This uses the apply id (the specific application/response record),
not the candidate's resume id -- those are different numbers in the API
payload and must not be confused.
"""
from __future__ import annotations

from src.integrations.robotaua_api import parse_apply


def _apply(**kw) -> dict:
    defaults = dict(
        id=92348749, resumeId=26145663, vacancyId=11249166,
        name="Тест Тестовий", phone="380990000001", resumeType="AttachedFile",
    )
    defaults.update(kw)
    return defaults


def test_resume_url_uses_apply_id_not_resume_id_in_new_format():
    fields = parse_apply(_apply())
    assert fields["resume_url"] == (
        "https://robota.ua/my/vacancies/all/applies?id=92348749-prof"
    )
    # the old candidate-id-based link must not appear anywhere in it
    assert "26145663" not in fields["resume_url"]
    assert "/candidates/" not in fields["resume_url"]


def test_resume_url_is_none_without_an_apply_id():
    fields = parse_apply(_apply(id=None))
    assert fields["resume_url"] is None


def test_resume_url_uses_apply_id_even_for_attached_file_with_zero_resume_id():
    # AttachedFile applies carry resumeId=0 (no linked CV record) -- the old
    # candidate-id link was silently None for these. The apply id always
    # exists regardless, so the new link must still be built.
    fields = parse_apply(_apply(id=92355425, resumeId=0))
    assert fields["resume_url"] == (
        "https://robota.ua/my/vacancies/all/applies?id=92355425-prof"
    )
