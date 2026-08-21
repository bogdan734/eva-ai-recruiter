"""Who is worth spending one of the account's paid contact openings on.

The rule used to be written for the sales funnel alone: whitelisted oblast plus
a sales/logistics job title. An intake-only vacancy was refused outright, on the
reasoning that nobody would call those applicants so revealing a number bought
nothing.

That reasoning does not survive contact with the client. On robota.ua most
records arrive as `Interaction` with the phone hidden, so refusing to open them
meant the bookkeeping vacancies produced almost no cards at all — which is what
the client noticed and wrote in about. The openings are prepaid and expire on a
date whether or not they are used, so the sales-shaped guards are the wrong
question for a vacancy a human works by hand.
"""
import dataclasses

import pytest

from src.common import vacancies
from src.integrations.robotaua_sync import worth_opening


def _apply(vacancy_id: int, speciality: str = "") -> dict:
    return {"id": 1, "vacancyId": vacancy_id, "speciality": speciality, "cityId": 4}


def _first_id(key: str) -> int:
    return sorted(vacancies.all_vacancies()[key].robotaua_ids)[0]


class TestIntakeOnlyVacancy:
    """A vacancy a recruiter works by hand carries its own portrait."""

    def test_opens_regardless_of_job_title(self, monkeypatch):
        vac = vacancies.all_vacancies()["accountant"]
        monkeypatch.setattr(
            vacancies, "for_robotaua", lambda _vid: _replace(vac, open_paid_contacts=True)
        )
        # "бухгалтер" is in none of the sales ROLE_MARKERS, and that is the point.
        assert worth_opening(_apply(1, "бухгалтер"), "Дніпропетровська") is True

    def test_refuses_when_there_is_nothing_to_judge_by(self, monkeypatch):
        """No title and no history means no opinion — and no opening.

        Openings come from a fixed prepaid pool, so an unreadable record loses
        to a legible one every time. 49 of the 175 parked records carry no
        speciality; they stay visible in the robota.ua cabinet either way.
        """
        vac = vacancies.all_vacancies()["accountant"]
        monkeypatch.setattr(
            vacancies, "for_robotaua", lambda _vid: _replace(vac, open_paid_contacts=True)
        )
        assert worth_opening(_apply(1, ""), "Дніпропетровська") is False

    def test_opens_an_on_role_applicant(self, monkeypatch):
        """Geo is not asked of a vacancy nobody dials — the title carries it."""
        vac = vacancies.all_vacancies()["accountant"]
        monkeypatch.setattr(
            vacancies, "for_robotaua", lambda _vid: _replace(vac, open_paid_contacts=True)
        )
        assert worth_opening(_apply(1, "Бухгалтер первинної документації"), None) is True

    def test_still_refuses_when_the_vacancy_says_no(self, monkeypatch):
        """The per-vacancy switch stays the veto — nothing here overrides it."""
        vac = vacancies.all_vacancies()["accountant"]
        monkeypatch.setattr(
            vacancies, "for_robotaua", lambda _vid: _replace(vac, open_paid_contacts=False)
        )
        assert worth_opening(_apply(1, "бухгалтер"), "Дніпропетровська") is False


class TestCalledVacancy:
    """A vacancy Eva dials keeps the old guards: a burnt opening is a wasted call."""

    def test_refuses_off_portrait_title(self, monkeypatch):
        vac = vacancies.all_vacancies()["sales"]
        monkeypatch.setattr(
            vacancies,
            "for_robotaua",
            lambda _vid: _replace(vac, open_paid_contacts=True, screen_enabled=True),
        )
        assert worth_opening(_apply(1, "перукар"), "Дніпропетровська") is False

    def test_accepts_on_portrait_title(self, monkeypatch):
        vac = vacancies.all_vacancies()["sales"]
        monkeypatch.setattr(
            vacancies,
            "for_robotaua",
            lambda _vid: _replace(vac, open_paid_contacts=True, screen_enabled=True),
        )
        assert worth_opening(_apply(1, "менеджер з продажу"), "Дніпропетровська") is True

    def test_refuses_without_a_region(self, monkeypatch):
        """Unknown oblast on a dialled vacancy is still a refusal."""
        vac = vacancies.all_vacancies()["sales"]
        monkeypatch.setattr(
            vacancies,
            "for_robotaua",
            lambda _vid: _replace(vac, open_paid_contacts=True, screen_enabled=True),
        )
        assert worth_opening(_apply(1, "менеджер з продажу"), None) is False


def _replace(vac, **changes):
    return dataclasses.replace(vac, **changes)


@pytest.mark.parametrize("key", ["sales", "accountant"])
def test_registry_ids_are_ints(key):
    """Guards the panel: ids typed by hand must never land as strings."""
    for vid in vacancies.all_vacancies()[key].robotaua_ids:
        assert isinstance(vid, int)
