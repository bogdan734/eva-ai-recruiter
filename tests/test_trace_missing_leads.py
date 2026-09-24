"""The trace answers "where did this named applicant stop" from the same facts
the pollers act on, so its verdicts have to follow the pollers' branches."""
from __future__ import annotations

from scripts.trace_missing_leads import (
    card_verdict,
    name_matches,
    robotaua_verdict,
    workua_verdict,
)
from src.integrations.workua_api import parse_response

ACCOUNTANT_WU = 8242731
SALES_RU = 11277559


class TestNameMatches:
    def test_order_and_patronymic_do_not_matter(self):
        assert name_matches("Сулім Олександр", "Олександр Сулім Петрович")

    def test_case_and_apostrophe_forms_do_not_matter(self):
        assert name_matches("Мар'яна Коваль", "КОВАЛЬ МАРʼЯНА")

    def test_every_word_must_be_there(self):
        assert not name_matches("Катерина Шевченко", "Катерина Матвійчук")

    def test_empty_name_never_matches(self):
        assert not name_matches("Інна", None)


def _wu(**kw):
    raw = {"id": "500", "job_id": str(ACCOUNTANT_WU), "candidate_id": "1",
           "fio": "Сулім Олександр", "phone": "+380671234567", "from_type": "send"}
    raw.update(kw)
    return parse_response(raw)


class TestWorkUaVerdict:
    def test_no_phone(self):
        assert workua_verdict(_wu(phone=None), allowed={ACCOUNTANT_WU}, cursor={}).startswith(
            "без телефону"
        )

    def test_unmapped_posting_waiting_on_the_ledger(self):
        cursor = {"responses_last_id": 900, "skipped_jobs": {"8999999": {"resume_from": 1}}}
        verdict = workua_verdict(_wu(job_id="8999999"), allowed={ACCOUNTANT_WU}, cursor=cursor)
        assert "8999999" in verdict and "skipped_jobs" in verdict

    def test_not_reached_by_the_cursor_yet(self):
        verdict = workua_verdict(_wu(id="950"), allowed={ACCOUNTANT_WU},
                                 cursor={"responses_last_id": 900})
        assert verdict.startswith("ще не опрацьовано")

    def test_handed_to_the_intake(self):
        verdict = workua_verdict(_wu(), allowed={ACCOUNTANT_WU}, cursor={"responses_last_id": 900})
        assert verdict.startswith("передано в інтейк")


def _ru(**kw):
    apply = {"id": 70, "name": "Катерина Шевченко", "vacancyId": SALES_RU,
             "resumeType": "Notepad", "phone": "", "addDate": "2026-09-24T09:00:00"}
    apply.update(kw)
    return apply


class TestRobotaUaVerdict:
    def test_interaction_is_not_a_response(self):
        assert robotaua_verdict(_ru(resumeType="Interaction"), allowed={SALES_RU},
                                cursor={}).startswith("Interaction")

    def test_unmapped_posting(self):
        verdict = robotaua_verdict(_ru(vacancyId=1), allowed={SALES_RU}, cursor={})
        assert verdict.startswith("вакансія 1 не в реєстрі")

    def test_parked(self):
        verdict = robotaua_verdict(_ru(), allowed={SALES_RU}, cursor={"pending": {"70": {}}})
        assert verdict.startswith("у черзі pending")

    def test_seen_without_a_phone(self):
        verdict = robotaua_verdict(_ru(), allowed={SALES_RU}, cursor={"seen_ids": [70]})
        assert verdict.startswith("опрацьовано без телефону")

    def test_walked_past_and_lost(self):
        cursor = {"last_add_date": "2026-09-24T12:00:00", "seen_ids": []}
        verdict = robotaua_verdict(_ru(phone="+380671234567"), allowed={SALES_RU}, cursor=cursor)
        assert verdict.startswith("курсор пройшов повз")


class TestCardVerdict:
    def test_row_without_a_card(self):
        assert card_verdict(None, None).startswith("картки немає")

    def test_deleted_card(self):
        assert card_verdict(11500, None).startswith("картку 11500 видалено")

    def test_live_card(self):
        assert card_verdict(11500, 6) == "картка 11500 у воронці 6"
