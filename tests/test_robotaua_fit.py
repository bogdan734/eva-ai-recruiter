"""How well does this applicant fit the vacancy, judged before paying to see them.

robota.ua hides the phone until a contact is opened, and openings come in a
fixed prepaid pool. So the decision has to be made on what arrives for free:
the desired speciality, the work history, robota.ua's own match flag and how
complete the CV is.

Everything here is deliberately readable from an `Interaction` record, which is
what most of the traffic is and which carries no phone at all.
"""
import dataclasses

import pytest

from src.common import vacancies
from src.integrations.robotaua_sync import fit_score

ACCOUNTANT_MARKERS = ("бухгалтер", "облік", "фінанс", "економіст", "казначей")


def _vac(**changes):
    base = vacancies.all_vacancies()["accountant"]
    changes.setdefault("role_markers", ACCOUNTANT_MARKERS)
    return dataclasses.replace(base, **changes)


def _apply(**fields) -> dict:
    base = {
        "id": 1,
        "vacancyId": 11304674,
        "speciality": "",
        "experiences": [],
        "isMatchVacancy": False,
        "summaryPercentage": 0,
    }
    base.update(fields)
    return base


class TestSpeciality:
    """The desired position is the strongest free signal there is."""

    def test_matching_speciality_scores_high(self):
        assert fit_score(_apply(speciality="Бухгалтер"), _vac()) >= 50

    def test_unrelated_speciality_scores_low(self):
        assert fit_score(_apply(speciality="Перукар"), _vac()) < 50

    def test_match_is_case_insensitive(self):
        hi = fit_score(_apply(speciality="БУХГАЛТЕР"), _vac())
        lo = fit_score(_apply(speciality="бухгалтер"), _vac())
        assert hi == lo >= 50

    def test_partial_word_counts(self):
        """«Головний бухгалтер-економіст» must not need an exact equality."""
        assert fit_score(_apply(speciality="Головний бухгалтер-економіст"), _vac()) >= 50


class TestExperience:
    """An empty speciality is normal on Interaction records — read the history."""

    def test_experience_alone_can_carry_a_record(self):
        appl = _apply(
            speciality="",
            experiences=[{"position": "Бухгалтер первинної документації"}],
        )
        assert fit_score(appl, _vac()) > 0

    def test_experience_does_not_outrank_speciality(self):
        by_spec = fit_score(_apply(speciality="Бухгалтер"), _vac())
        by_exp = fit_score(
            _apply(speciality="", experiences=[{"position": "Бухгалтер"}]), _vac()
        )
        assert by_spec > by_exp


class TestTiebreakers:
    def test_platform_match_flag_adds_weight(self):
        plain = fit_score(_apply(speciality="Бухгалтер"), _vac())
        flagged = fit_score(_apply(speciality="Бухгалтер", isMatchVacancy=True), _vac())
        assert flagged > plain

    def test_fuller_cv_wins_between_equals(self):
        """The 48%-complete CV in the client's screenshot is the weaker bet."""
        thin = fit_score(_apply(speciality="Бухгалтер", summaryPercentage=48), _vac())
        full = fit_score(_apply(speciality="Бухгалтер", summaryPercentage=85), _vac())
        assert full > thin

    def test_completeness_alone_never_qualifies(self):
        """A perfectly filled CV for the wrong job is still the wrong job."""
        assert fit_score(_apply(speciality="Перукар", summaryPercentage=100), _vac()) < 50


class TestBounds:
    def test_never_exceeds_one_hundred(self):
        appl = _apply(
            speciality="Бухгалтер, облік, фінанси",
            experiences=[{"position": "Головний бухгалтер"}],
            isMatchVacancy=True,
            summaryPercentage=100,
        )
        assert 0 <= fit_score(appl, _vac()) <= 100

    def test_never_negative(self):
        assert fit_score(_apply(), _vac()) >= 0

    def test_no_markers_configured_is_neutral_not_zero(self):
        """A vacancy nobody has described yet must not block every opening."""
        assert fit_score(_apply(speciality="будь-що"), _vac(role_markers=())) >= 50


@pytest.mark.parametrize("key", ["sales", "accountant"])
def test_shipped_vacancies_describe_their_role(key):
    """Both live vacancies carry markers, or scoring silently degrades."""
    assert vacancies.all_vacancies()[key].role_markers
