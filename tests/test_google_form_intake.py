"""Form submissions reaching the CRM without anyone's credentials.

The rows stopped arriving in early September and were entered by hand for ten
days. Every pull route is closed — no service account, Google rejects the
recruiter's cookies from this IP, and the sheet holds applicants' phone numbers
so it cannot be made public — so the form pushes to us instead.

What these pin is the part that broke the old integration quietly: field
mapping. A form gets edited, and anything keyed on column order starts filing
blanks without raising.
"""
from __future__ import annotations

import pytest

from src.api import services
from src.common.keycrm import crm_source_id


ANSWERS = {
    "Позначка часу": "18.09.2026 11:42:03",
    "Ваше ПІБ": "Марченко Ольга Петрівна",
    "Номер телефону": "0501461979",
    "Місто проживання": "Рівне",
    "На яку посаду претендуєте?": "Менеджер з продажу",
    "Ваш вік": "34",
    "Електронна пошта": "olga@example.com",
    "Чи готові працювати позмінно?": "Так, готова",
}


# ------------------------------------------------------------------- mapping

@pytest.mark.parametrize(
    "field,expected",
    [
        ("full_name", "Марченко Ольга Петрівна"),
        ("phone", "0501461979"),
        ("region", "Рівне"),
        ("position", "Менеджер з продажу"),
        ("email", "olga@example.com"),
    ],
)
def test_each_field_is_found_by_what_the_question_asks(field, expected):
    assert services._pick_form_field(ANSWERS, field) == expected


def test_reordering_the_form_changes_nothing():
    """The whole point of keying on question text: she edits this form."""
    shuffled = dict(reversed(list(ANSWERS.items())))
    assert services._pick_form_field(shuffled, "phone") == "0501461979"
    assert services._pick_form_field(shuffled, "full_name") == "Марченко Ольга Петрівна"


def test_russian_wording_maps_too():
    ru = {"Ваше ФИО": "Иванова Анна", "Контактный телефон": "0671234567", "Город": "Ровно"}
    assert services._pick_form_field(ru, "full_name") == "Иванова Анна"
    assert services._pick_form_field(ru, "phone") == "0671234567"
    assert services._pick_form_field(ru, "region") == "Ровно"


def test_a_blank_answer_is_not_treated_as_an_answer():
    assert services._pick_form_field({"Ваше ПІБ": "   "}, "full_name") is None


def test_unmapped_answers_are_kept_on_the_card():
    """The shift-work answer has no field of its own and is often the deciding
    one — the card must not be poorer than the spreadsheet row."""
    text = services._form_resume_text(ANSWERS)
    assert "Чи готові працювати позмінно?: Так, готова" in text
    assert "Ваше ПІБ: Марченко Ольга Петрівна" in text


def test_empty_answers_are_left_out_of_the_card_text():
    assert "порожнє" not in services._form_resume_text({"порожнє": "", "Ваше ПІБ": "Х"})


# -------------------------------------------------------------------- intake

@pytest.mark.asyncio
async def test_a_submission_becomes_one_card(monkeypatch):
    seen = {}

    class _Router:
        async def ingest(self, payload):
            seen["payload"] = payload
            return type("R", (), {
                "accepted": True, "duplicate": False,
                "candidate_id": 77, "keycrm_lead_id": 11500, "reason": "",
            })()

    monkeypatch.setattr("src.api.inbound_router.InboundRouter", _Router)
    out = await services.handle_google_form_submission(ANSWERS)

    assert out["ok"] is True and out["lead_id"] == 11500
    p = seen["payload"]
    assert p.full_name == "Марченко Ольга Петрівна"
    assert p.phone_raw == "0501461979"
    assert p.source == "googleform"
    # The person applied to us, so the 04.09 policy parks them for a human and
    # Єва never cold-calls them.
    assert p.is_response is True


@pytest.mark.asyncio
async def test_a_submission_with_no_phone_is_refused_not_filed(monkeypatch):
    """Silence was the old failure mode. A card with no phone is unusable, and
    a form edited out of shape must be visible, not absorbed."""
    called = False

    class _Router:
        async def ingest(self, payload):
            nonlocal called
            called = True

    monkeypatch.setattr("src.api.inbound_router.InboundRouter", _Router)
    out = await services.handle_google_form_submission({"Ваше ПІБ": "Хтось"})
    assert out["ok"] is False
    assert called is False


# -------------------------------------------------------------------- source

def test_form_cards_are_filed_under_the_label_she_filters_by():
    assert crm_source_id("googleform") == 3


def test_the_other_sources_are_untouched():
    assert crm_source_id("workua") == 1
    assert crm_source_id("robotaua") == 2
    assert crm_source_id("telegram") == 4
