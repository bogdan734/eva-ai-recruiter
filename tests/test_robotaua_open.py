"""Who is worth spending one of the account's paid contact openings on.

2026-09-04 policy change (user-directed): every candidate who applied to a
vacancy himself must reach the CRM, full stop -- age/region/role screening
no longer decides who gets a card, only who Єва calls. `worth_opening()`
used to re-run that same screening one step earlier, before we even had a
phone number to attach a card to, which produced the same silent drop the
policy change was meant to stop. The only thing left worth gating a paid
open on is the vacancy's own on/off switch (`open_paid_contacts`) -- a
recruiter's cost/feature toggle per vacancy, not a portrait match on the
applicant.

2026-09-04 fix, same day: removing the region/role guards also removed the
only thing that told `Interaction` records (robota.ua surfacing a view or an
algorithmic recommendation -- not a response at all) apart from a genuine
apply. `TestRefusesNonResponses` below covers that guard: it is not a
portrait filter (no age/region/role), it is the "did this person actually
apply" signal, which the policy change was never meant to remove.
"""
import dataclasses

import pytest

from src.common import vacancies
from src.integrations.robotaua_sync import worth_opening


def _apply(vacancy_id: int, speciality: str = "", resume_type: str = "AttachedFile") -> dict:
    # Default resume_type is a genuine-response type so every pre-existing
    # test case below keeps meaning "a real apply" unless it says otherwise.
    return {
        "id": 1,
        "vacancyId": vacancy_id,
        "speciality": speciality,
        "cityId": 4,
        "resumeType": resume_type,
    }


def _replace(vac, **changes):
    return dataclasses.replace(vac, **changes)


class TestOpensRegardlessOfPortrait:
    """Region/role/title no longer gate a paid open -- only the switch (and
    genuine-response check below) does."""

    def test_opens_off_portrait_title(self, monkeypatch):
        vac = vacancies.all_vacancies()["sales"]
        monkeypatch.setattr(
            vacancies,
            "for_robotaua",
            lambda _vid: _replace(vac, open_paid_contacts=True, screen_enabled=True),
        )
        # "перукар" (hairdresser) matches none of the sales ROLE_MARKERS --
        # used to be refused, must now be opened anyway.
        assert worth_opening(_apply(1, "перукар"), "Дніпропетровська") is True

    def test_opens_without_a_region(self, monkeypatch):
        vac = vacancies.all_vacancies()["sales"]
        monkeypatch.setattr(
            vacancies,
            "for_robotaua",
            lambda _vid: _replace(vac, open_paid_contacts=True, screen_enabled=True),
        )
        # No region info at all -- used to be a refusal, must now open anyway.
        assert worth_opening(_apply(1, "менеджер з продажу"), None) is True

    def test_opens_a_blocked_region(self, monkeypatch):
        vac = vacancies.all_vacancies()["sales"]
        monkeypatch.setattr(
            vacancies,
            "for_robotaua",
            lambda _vid: _replace(vac, open_paid_contacts=True, screen_enabled=True),
        )
        # Kyiv is in regions_blocked -- irrelevant now, must still open.
        assert worth_opening(_apply(1, "менеджер з продажу"), "Київська") is True

    def test_opens_intake_only_vacancy_with_no_title_to_judge_by(self, monkeypatch):
        """49 of the 175 parked records used to carry no speciality and were
        refused outright. They must open too now -- nothing left to judge them
        against except whether the vacancy wants paid opens and whether this
        is a real apply."""
        vac = vacancies.all_vacancies()["accountant"]
        monkeypatch.setattr(
            vacancies, "for_robotaua", lambda _vid: _replace(vac, open_paid_contacts=True)
        )
        assert worth_opening(_apply(1, "", resume_type="Notepad"), "Дніпропетровська") is True


class TestVacancySwitchStillVetoes:
    """A vacancy that opted out of paid opens is still refused."""

    def test_refuses_when_the_vacancy_says_no(self, monkeypatch):
        vac = vacancies.all_vacancies()["accountant"]
        monkeypatch.setattr(
            vacancies, "for_robotaua", lambda _vid: _replace(vac, open_paid_contacts=False)
        )
        assert worth_opening(_apply(1, "бухгалтер"), "Дніпропетровська") is False

    def test_refuses_when_the_sales_vacancy_says_no(self, monkeypatch):
        vac = vacancies.all_vacancies()["sales"]
        monkeypatch.setattr(
            vacancies,
            "for_robotaua",
            lambda _vid: _replace(vac, open_paid_contacts=False, screen_enabled=True),
        )
        assert worth_opening(_apply(1, "менеджер з продажу"), "Дніпропетровська") is False


class TestRefusesNonResponses:
    """`Interaction` is robota.ua showing a view or a recommendation, not a
    reply -- the ground-truth "did they actually apply" signal, which the
    policy change never meant to remove. Regression coverage for the incident
    where Клецко Ігор, Недоступ Олексій and Blanar Fedir (none of them
    remotely sales/logistics) got their contacts opened and cards created
    within minutes of the region/role guards being removed."""

    def test_refuses_an_interaction_record_even_off_portrait(self, monkeypatch):
        vac = vacancies.all_vacancies()["sales"]
        monkeypatch.setattr(
            vacancies,
            "for_robotaua",
            lambda _vid: _replace(vac, open_paid_contacts=True, screen_enabled=True),
        )
        apply = _apply(1, "юрист, митний брокер", resume_type="Interaction")
        assert worth_opening(apply, "Дніпропетровська") is False

    def test_refuses_an_interaction_record_with_no_region_either(self, monkeypatch):
        vac = vacancies.all_vacancies()["sales"]
        monkeypatch.setattr(
            vacancies,
            "for_robotaua",
            lambda _vid: _replace(vac, open_paid_contacts=True, screen_enabled=True),
        )
        assert worth_opening(_apply(1, "", resume_type="Interaction"), None) is False

    @pytest.mark.parametrize("resume_type", ["AttachedFile", "Notepad"])
    def test_still_opens_genuine_response_types(self, monkeypatch, resume_type):
        vac = vacancies.all_vacancies()["sales"]
        monkeypatch.setattr(
            vacancies,
            "for_robotaua",
            lambda _vid: _replace(vac, open_paid_contacts=True, screen_enabled=True),
        )
        apply = _apply(1, "перукар", resume_type=resume_type)
        assert worth_opening(apply, "Дніпропетровська") is True

    def test_vacancy_switch_still_wins_over_a_genuine_response(self, monkeypatch):
        """open_paid_contacts=False refuses even AttachedFile/Notepad -- the
        switch and the response check are independent vetoes, either one
        alone is enough to refuse."""
        vac = vacancies.all_vacancies()["sales"]
        monkeypatch.setattr(
            vacancies,
            "for_robotaua",
            lambda _vid: _replace(vac, open_paid_contacts=False, screen_enabled=True),
        )
        apply = _apply(1, "менеджер з продажу", resume_type="AttachedFile")
        assert worth_opening(apply, "Дніпропетровська") is False


@pytest.mark.parametrize("key", ["sales", "accountant"])
def test_registry_ids_are_ints(key):
    """Guards the panel: ids typed by hand must never land as strings."""
    for vid in vacancies.all_vacancies()[key].robotaua_ids:
        assert isinstance(vid, int)
