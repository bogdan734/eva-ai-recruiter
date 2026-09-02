"""«Номер вакансії» and «Посилання на вакансію» on a freshly created card.

Both were empty on every robota.ua card, which is what the recruiter reported.
The reason was that nobody passed them: LD_1002 only ever received a work.ua
response id, and LD_1004 received the applicant's résumé link — useful, but not
what a field called «Посилання на вакансію» promises.

A card must point at the posting the person actually answered, on the board they
answered it on. The vacancy can carry several ids per board, so the id has to
travel with the applicant rather than be guessed from the vacancy.
"""
import dataclasses

import pytest

from src.common import vacancies
from src.common.vacancy_link import vacancy_number_and_url


def _route(**changes):
    base = vacancies.all_vacancies()["accountant"]
    return dataclasses.replace(base, **changes)


class TestRobotaUa:
    def test_number_is_the_board_id(self):
        num, _ = vacancy_number_and_url("robotaua_response", 11304674, _route())
        assert num == "11304674"

    def test_url_points_at_that_posting(self):
        _, url = vacancy_number_and_url("robotaua_response", 11304674, _route())
        assert "robota.ua" in url
        assert "11304674" in url

    def test_chat_source_counts_as_robotaua(self):
        num, url = vacancy_number_and_url("robotaua_chat", 11292426, _route())
        assert num == "11292426"
        assert "robota.ua" in url


class TestWorkUa:
    def test_number_and_url_use_the_job_id(self):
        num, url = vacancy_number_and_url("workua_response_send", 8409676, _route())
        assert num == "8409676"
        assert "work.ua" in url
        assert "8409676" in url

    def test_never_returns_the_other_board(self):
        _, url = vacancy_number_and_url("workua_response_send", 8409676, _route())
        assert "robota.ua" not in url


class TestFallback:
    """No board id — fall back to whatever the vacancy itself carries."""

    def test_falls_back_to_the_vacancy_fields(self):
        route = _route(vacancy_number="8242731", vacancy_url="https://www.work.ua/jobs/8242731/")
        num, url = vacancy_number_and_url("manual", None, route)
        assert num == "8242731"
        assert url == "https://www.work.ua/jobs/8242731/"

    def test_empty_vacancy_fields_give_empty_strings(self):
        route = _route(vacancy_number="", vacancy_url="")
        assert vacancy_number_and_url("manual", None, route) == ("", "")

    @pytest.mark.parametrize("source", [None, "", "inbound_call"])
    def test_unknown_source_with_an_id_still_falls_back(self, source):
        """An id we cannot attribute to a board must not invent a link."""
        route = _route(vacancy_number="X", vacancy_url="https://example.test/")
        num, url = vacancy_number_and_url(source, 12345, route)
        assert (num, url) == ("X", "https://example.test/")


class TestCombinedSources:
    def test_first_recognised_board_wins(self):
        """`source` accumulates channels; the card is made at first contact."""
        num, url = vacancy_number_and_url(
            "robotaua_response,workua_response_send", 11304674, _route()
        )
        assert num == "11304674"
        assert "robota.ua" in url
