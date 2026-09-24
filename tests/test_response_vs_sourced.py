"""2026-09-04 policy change (user-directed): screening (region/name portrait)
must decide only who Єва calls, never who reaches the CRM. Someone who
applied to a vacancy himself (work.ua/robota.ua response, robota.ua chat --
IngestPayload.is_response=True) always gets a card and never enters the call
queue, regardless of region/role/age and regardless of the vacancy's own
calls_enabled flag. Someone Єва is actively sourcing (workua_scraper,
workua_search -- is_response=False, the default) keeps the old behaviour
unchanged: the portrait filter still decides whether they get a card at all,
because the only reason a sourced candidate exists in our system is for Єва
to call them.

These two decisions were pulled out of InboundRouter.ingest() into
screening_applies() / intake_status() specifically so they could be unit
tested without a database or a live KeyCRM client.
"""
import dataclasses

from src.api.inbound_router import IngestPayload, intake_status, screening_applies
from src.common import vacancies
from src.common.models import CandidateStatus


def _route(**changes):
    base = vacancies.all_vacancies()["sales"]
    return dataclasses.replace(base, **changes)


class TestScreeningApplies:
    def test_skipped_for_a_response_even_on_a_screened_vacancy(self):
        route = _route(screen_enabled=True, calls_enabled=True)
        payload = IngestPayload(full_name="X", phone_raw="+380991112233", is_response=True)
        assert screening_applies(route, payload) is False

    def test_still_runs_for_a_sourced_candidate_on_a_screened_vacancy(self):
        route = _route(screen_enabled=True, calls_enabled=True)
        payload = IngestPayload(full_name="X", phone_raw="+380991112233", is_response=False)
        assert screening_applies(route, payload) is True

    def test_never_runs_on_an_intake_only_vacancy_either_way(self):
        route = _route(screen_enabled=False, calls_enabled=False)
        for is_response in (True, False):
            payload = IngestPayload(
                full_name="X", phone_raw="+380991112233", is_response=is_response
            )
            assert screening_applies(route, payload) is False


class TestIntakeStatus:
    def test_response_never_enters_the_call_queue_even_when_calls_enabled(self):
        """The core of the policy change: applying himself must never put a
        candidate in Єва's dial queue, no matter how call-friendly the
        vacancy is."""
        route = _route(calls_enabled=True)
        payload = IngestPayload(full_name="X", phone_raw="+380991112233", is_response=True)
        assert intake_status(route, payload) == CandidateStatus.MANAGER_REVIEW

    def test_sourced_candidate_on_a_calling_vacancy_still_gets_dialed(self):
        route = _route(calls_enabled=True)
        payload = IngestPayload(full_name="X", phone_raw="+380991112233", is_response=False)
        assert intake_status(route, payload) == CandidateStatus.NEW_RESUME

    def test_sourced_candidate_on_a_non_calling_vacancy_stays_manager_review(self):
        route = _route(calls_enabled=False)
        payload = IngestPayload(full_name="X", phone_raw="+380991112233", is_response=False)
        assert intake_status(route, payload) == CandidateStatus.MANAGER_REVIEW


def test_is_response_defaults_to_false():
    # Sanity check on the dataclass default: every existing caller that never
    # sets is_response (the cold-sourcing paths) keeps today's behaviour.
    payload = IngestPayload(full_name="X", phone_raw="+380991112233")
    assert payload.is_response is False
