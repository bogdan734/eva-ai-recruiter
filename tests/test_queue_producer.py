"""feed_resumes_to_pipeline (src/scraper/queue_producer.py) — the cold-sourced
listing -> IngestPayload step. Covers the vacancy_key/source/is_response
threading added for cold sourcing: a candidate found via work.ua search for
"logistics_jr" must land on the logistics_jr script, not silently fall back to
the sales default, and must never be mistaken for a warm "response" (which
would route them to manager_review instead of Єва's call queue)."""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from src.scraper import queue_producer
from src.scraper.workua import ResumeListing


def _listing(**kw) -> ResumeListing:
    defaults = dict(
        work_ua_url="https://www.work.ua/resumes/1/",
        full_name="Тест Тестовий", desired_position="Менеджер з продажу",
        region="Львів", experience_years=2, languages=["uk"],
        phone_e164="+380990000001",
    )
    defaults.update(kw)
    return ResumeListing(**defaults)


class _FakeScore:
    def __init__(self, score):
        self.score = score


class _FakeScorer:
    def __init__(self, score=0.9):
        self._score = score

    async def score(self, vacancy_text, candidate_text):
        return _FakeScore(self._score)


class _FakeRouter:
    def __init__(self):
        self.payloads = []

    async def ingest(self, payload):
        self.payloads.append(payload)
        return SimpleNamespace(accepted=True, duplicate=False, candidate_id=1)


@pytest.fixture(autouse=True)
def _accept_every_profile(monkeypatch):
    # profile_filter.evaluate is exercised by its own tests; isolate this
    # module's behaviour from it here.
    monkeypatch.setattr(
        queue_producer, "profile_evaluate",
        lambda **kw: SimpleNamespace(accepted=True, reason=""),
    )


@pytest.mark.asyncio
async def test_vacancy_key_and_source_thread_through_to_ingest_payload():
    router = _FakeRouter()
    stats = await queue_producer.feed_resumes_to_pipeline(
        [_listing()],
        vacancy_id=1,
        vacancy_text="Менеджер з логістики",
        vacancy_key="logistics_jr",
        scorer=_FakeScorer(0.9),
        router=router,
    )
    assert stats["ingested"] == 1
    assert len(router.payloads) == 1
    payload = router.payloads[0]
    assert payload.vacancy_key == "logistics_jr"
    assert payload.source == "workua_search"
    # Cold-sourced: never a "response" -- that would wrongly route to
    # manager_review and skip Єва's call queue entirely.
    assert payload.is_response is False


@pytest.mark.asyncio
async def test_no_vacancy_key_falls_back_to_ingest_payload_default():
    router = _FakeRouter()
    await queue_producer.feed_resumes_to_pipeline(
        [_listing()], vacancy_id=1, vacancy_text="Менеджер з продажу",
        scorer=_FakeScorer(0.9), router=router,
    )
    from src.common import vacancies
    assert router.payloads[0].vacancy_key == vacancies.DEFAULT.key


@pytest.mark.asyncio
async def test_listing_without_phone_is_rejected_before_scoring():
    router = _FakeRouter()
    scored = []
    scorer = _FakeScorer(0.9)
    orig_score = scorer.score

    async def _tracking_score(*a, **kw):
        scored.append(1)
        return await orig_score(*a, **kw)

    scorer.score = _tracking_score
    stats = await queue_producer.feed_resumes_to_pipeline(
        [_listing(phone_e164=None)], vacancy_id=1, vacancy_text="x",
        scorer=scorer, router=router,
    )
    assert stats["rejected"] == 1
    assert scored == []  # never even reaches the paid scoring call
    assert router.payloads == []


@pytest.mark.asyncio
async def test_below_threshold_score_is_rejected_not_ingested():
    router = _FakeRouter()
    stats = await queue_producer.feed_resumes_to_pipeline(
        [_listing()], vacancy_id=1, vacancy_text="x",
        scorer=_FakeScorer(0.1), router=router, score_threshold=0.65,
    )
    assert stats["rejected"] == 1
    assert stats["matched"] == 0
    assert router.payloads == []
