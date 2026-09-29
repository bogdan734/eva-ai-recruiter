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
        "https://robota.ua/my/vacancies/all/applies?id=92348749-attach"
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
        "https://robota.ua/my/vacancies/all/applies?id=92355425-attach"
    )


def test_resume_url_suffix_follows_the_kind_of_resume():
    # 29.09: the cabinet addresses an apply as "{id}-prof" when the person
    # applied with a robota.ua profile resume and "{id}-attach" when they sent
    # a file. "-prof" on a file apply opens "Candidate was not found".
    assert parse_apply(_apply(id=1, resumeType="Notepad"))["resume_url"].endswith("?id=1-prof")
    assert parse_apply(_apply(id=2, resumeType="AttachedFile"))["resume_url"].endswith("?id=2-attach")
    assert parse_apply(_apply(id=3, resumeType=None))["resume_url"].endswith("?id=3-prof")


def test_nameless_file_apply_is_named_after_the_cv_file():
    # An unregistered applicant sends only a file; robota.ua then has no name.
    fields = parse_apply(_apply(name="", resumeType="AttachedFile",
                                fileName="Merve galuzynska (3).pdf (16).pdf"))
    assert fields["full_name"] == "Merve Galuzynska"


def test_generic_cv_file_name_is_not_taken_for_a_name():
    for file_name in ("CV.pdf", "Резюме (2).docx", "resume_final_2026.pdf", "IMG_2044.jpg", None):
        fields = parse_apply(_apply(name="", fileName=file_name))
        assert fields["full_name"] == "Кандидат robota.ua", file_name


def test_name_from_robota_ua_wins_over_the_file_name():
    fields = parse_apply(_apply(name="Наталья Денисюк", fileName="Денисюк Наталья Вікторівна 4 (1).docx"))
    assert fields["full_name"] == "Наталья Денисюк"
