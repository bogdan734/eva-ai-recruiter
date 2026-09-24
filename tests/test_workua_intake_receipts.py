"""Every work.ua response that does not become a card leaves a line saying who
and why.

On 24.09 the recruiter counted four responses in the cabinet and three cards.
The poller had only a `rejected` counter for the fourth: a response without a
phone and a response the intake turned down both went by without a name, a job
id or a reason anywhere in the logs, so nobody could say which of the two it was.
"""
from __future__ import annotations

import pytest

from src.api.inbound_router import IngestResult
from src.integrations import workua_sync as ws
from src.integrations.workua_api import parse_response


def _raw(**kw) -> dict:
    raw = {
        "id": "541000001",
        "job_id": "8242731",
        "candidate_id": "77",
        "fio": "Сулім Олександр",
        "phone": "+380671234567",
        "from_type": "send",
        "type": "resume",
        "with_file": "0",
    }
    raw.update(kw)
    return raw


class _Router:
    def __init__(self, result: IngestResult):
        self.result = result
        self.calls = 0

    async def ingest(self, payload):
        self.calls += 1
        return self.result


@pytest.fixture
def warnings(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(ws.log, "warning", lambda event, **kw: seen.append((event, kw)))
    return seen


@pytest.mark.asyncio
async def test_a_response_without_a_phone_is_named_in_the_log(warnings):
    router = _Router(IngestResult(accepted=True))
    stats = ws.PollStats()

    await ws._ingest_response(parse_response(_raw(phone=None)), router=router, stats=stats)

    assert router.calls == 0
    assert stats.no_phone == 1
    assert warnings == [
        (
            "workua.response_no_phone",
            {"response_id": 541000001, "job_id": 8242731, "name": "Сулім Олександр",
             "type": "resume"},
        )
    ]


@pytest.mark.asyncio
async def test_a_turned_down_response_says_why(warnings):
    router = _Router(IngestResult(accepted=False, reason="card_check_unavailable"))
    stats = ws.PollStats()

    await ws._ingest_response(parse_response(_raw()), router=router, stats=stats)

    assert stats.rejected == 1
    assert [(e, kw["reason"], kw["name"]) for e, kw in warnings] == [
        ("workua.ingest_rejected", "card_check_unavailable", "Сулім Олександр")
    ]


@pytest.mark.asyncio
async def test_an_accepted_response_stays_quiet(warnings):
    router = _Router(IngestResult(accepted=True, candidate_id=1))
    stats = ws.PollStats()

    await ws._ingest_response(parse_response(_raw()), router=router, stats=stats)

    assert stats.accepted == 1
    assert warnings == []
