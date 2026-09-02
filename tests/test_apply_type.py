"""An actual application must outrank a passive interaction.

robota.ua sends three kinds of record and our scoring treated them alike, which
produced exactly the inversion the recruiter reported on 2026-09-02: Жукова, who
only *interacted* with the posting, scored 70 and got a card; Тимошенко, who
applied with a CV attached, scored 15 and never reached the CRM. The recruiter
could not find the first in her responses list and could not find the second in
the funnel — both true, and both backwards.

`Interaction` is a view or a recommendation, not an application. `AttachedFile`
and `Notepad` are someone deliberately answering the posting. That difference is
the strongest free signal robota.ua gives us, and it was being ignored.
"""
import dataclasses

import pytest

from src.common import vacancies
from src.integrations.robotaua_sync import fit_score

MARKERS = ("логіст", "продаж", "менеджер")


def _route(**changes):
    base = vacancies.all_vacancies()["sales"]
    changes.setdefault("role_markers", MARKERS)
    return dataclasses.replace(base, **changes)


def _apply(resume_type="Interaction", **fields):
    base = {
        "id": 1,
        "vacancyId": 11277559,
        "resumeType": resume_type,
        "speciality": "",
        "experiences": [],
        "isMatchVacancy": False,
        "summaryPercentage": 0,
    }
    base.update(fields)
    return base


class TestApplicationBeatsInteraction:
    def test_a_real_application_outscores_a_view_of_equal_content(self):
        applied = fit_score(_apply("AttachedFile", speciality="Логіст"), _route())
        viewed = fit_score(_apply("Interaction", speciality="Логіст"), _route())
        assert applied > viewed

    @pytest.mark.parametrize("kind", ["AttachedFile", "Notepad"])
    def test_both_application_kinds_count(self, kind):
        applied = fit_score(_apply(kind, speciality="Логіст"), _route())
        viewed = fit_score(_apply("Interaction", speciality="Логіст"), _route())
        assert applied > viewed

    def test_the_reported_inversion_is_gone(self):
        """Тимошенко applied with a CV and no speciality; Жукова only looked."""
        timoshenko = fit_score(_apply("AttachedFile"), _route())
        zhukova = fit_score(_apply("Interaction", speciality="Логіст"), _route())
        # Не вимагаємо, щоб відгук переміг завжди — лише щоб він перестав бути
        # безнадійним: людина, яка справді відгукнулась, мусить проходити поріг.
        assert timoshenko >= 50
        assert zhukova >= 50


class TestStillJudgesContent:
    def test_applying_for_an_unrelated_job_still_scores_low(self):
        """A real application to the wrong role is still the wrong role."""
        assert fit_score(_apply("AttachedFile", speciality="Перукар"), _route()) < 50

    def test_interaction_with_matching_speciality_still_qualifies(self):
        assert fit_score(_apply("Interaction", speciality="Менеджер з продажу"), _route()) >= 50


class TestBounds:
    def test_stays_in_range(self):
        best = _apply(
            "AttachedFile",
            speciality="Логіст, менеджер з продажу",
            experiences=[{"position": "Логіст"}],
            isMatchVacancy=True,
            summaryPercentage=100,
        )
        assert 0 <= fit_score(best, _route()) <= 100

    def test_unknown_type_is_treated_as_a_view(self):
        odd = fit_score(_apply("СhoseSomethingElse", speciality="Логіст"), _route())
        viewed = fit_score(_apply("Interaction", speciality="Логіст"), _route())
        assert odd == viewed
