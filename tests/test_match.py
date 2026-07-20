import pytest

from src.match.scorer import FakeMatchScorer


@pytest.mark.asyncio
async def test_fake_scorer_overlap():
    scorer = FakeMatchScorer()
    res = await scorer.score(
        vacancy_text="Python developer with FastAPI and Postgres",
        candidate_text="I am a Python developer with FastAPI experience",
    )
    assert 0.0 < res.score <= 1.0
    assert "stub" in res.rationale


@pytest.mark.asyncio
async def test_fake_scorer_no_overlap():
    scorer = FakeMatchScorer()
    res = await scorer.score(
        vacancy_text="Civil engineer for bridge construction",
        candidate_text="Marketing manager with social media skills",
    )
    assert res.score < 0.3
