"""The loss this guards against: a response to a vacancy nobody had registered
yet is skipped, the cursor moves past it, and it is gone for good.

That is how 74% of the work.ua flow vanished unnoticed for weeks, and how seven
accountants vanished again on 2026-08-18 — three minutes before their job id was
added in the panel. A republished posting always arrives as an unknown id first,
so the skip has to be remembered, not just logged, and replayed on its own the
moment the id becomes known.
"""
import pytest

from src.api.inbound_router import IngestResult
from src.integrations import workua_sync as ws


def test_skip_is_recorded_with_a_replay_point_one_below_the_earliest_loss():
    cursor: dict = {}
    ws._record_skipped(cursor, job_id=8409676, response_id=539438740)
    ws._record_skipped(cursor, job_id=8409676, response_id=539438700)

    entry = cursor["skipped_jobs"]["8409676"]
    # last_id is exclusive, so replaying the earliest loss needs one below it.
    assert entry["resume_from"] == 539438699
    assert entry["count"] == 2


def test_a_later_skip_never_raises_the_replay_point():
    """Order of arrival must not cost us the earliest response."""
    cursor: dict = {}
    ws._record_skipped(cursor, job_id=8409676, response_id=100)
    ws._record_skipped(cursor, job_id=8409676, response_id=900)

    assert cursor["skipped_jobs"]["8409676"]["resume_from"] == 99


def test_catch_up_is_due_only_for_a_job_that_became_known(monkeypatch):
    cursor = {
        "skipped_jobs": {
            "8409676": {"resume_from": 99, "count": 7},
            "5466479": {"resume_from": 200, "count": 3},
        }
    }
    monkeypatch.setattr(
        ws.vacancies, "for_workua", lambda jid: object() if int(jid) == 8409676 else None
    )

    assert ws._due_catch_ups(cursor) == [(8409676, 99)]


def test_unknown_jobs_stay_on_the_ledger_until_someone_maps_them(monkeypatch):
    cursor = {"skipped_jobs": {"5466479": {"resume_from": 200, "count": 3}}}
    monkeypatch.setattr(ws.vacancies, "for_workua", lambda jid: None)

    assert ws._due_catch_ups(cursor) == []
    assert "5466479" in cursor["skipped_jobs"]


class _FakeClient:
    """Serves one page of responses per last_id, like the real feed."""

    def __init__(self, items):
        self._items = items
        self.calls: list[int | None] = []

    async def list_responses(self, *, limit=50, last_id=None, from_types=None, sort=1):
        self.calls.append(last_id)
        after = [i for i in self._items if int(i["id"]) > (last_id or 0)]
        return {"status": "ok", "items": after[:limit]}


class _FakeRouter:
    def __init__(self):
        self.seen: list[str] = []

    async def ingest(self, payload):
        self.seen.append(payload.workua_response_id)
        return IngestResult(accepted=True)


def _resp(rid: int, job_id: int) -> dict:
    return {
        "id": str(rid),
        "job_id": str(job_id),
        "candidate_id": "1",
        "fio": "Іван Петренко",
        "phone": "+380671234567",
        "from_type": "send",
        "type": "resume",
        "with_file": "0",
    }


@pytest.mark.asyncio
async def test_catch_up_replays_only_the_recovered_job_and_clears_the_ledger(monkeypatch):
    """The whole point: nobody has to press anything for the skipped seven."""
    items = [_resp(100, 8409676), _resp(101, 5466479), _resp(102, 8409676)]
    client, router = _FakeClient(items), _FakeRouter()
    cursor = {
        "responses_last_id": 102,
        "skipped_jobs": {"8409676": {"resume_from": 99, "count": 2}},
    }

    class _Route:
        key = "accountant"
        screen_enabled = False

    monkeypatch.setattr(
        ws.vacancies, "for_workua", lambda jid: _Route() if int(jid) == 8409676 else None
    )

    recovered = await ws.catch_up_skipped(client=client, router=router, cursor=cursor)

    assert router.seen == ["100", "102"]  # 101 belongs to a job we do not recruit for
    assert recovered == 2
    assert cursor["skipped_jobs"] == {}
    assert cursor["responses_last_id"] == 102  # replay must not move the live cursor


@pytest.mark.asyncio
async def test_each_recovered_job_is_counted_on_its_own(monkeypatch):
    """The per-job receipt must not accumulate across jobs.

    A running total made the second posting look like it gave back everything
    the first one did, which is exactly the number somebody would quote when
    deciding whether a panel edit worked.
    """
    items = [_resp(10, 111), _resp(11, 222), _resp(12, 222)]
    client, router = _FakeClient(items), _FakeRouter()
    cursor = {
        "responses_last_id": 12,
        "skipped_jobs": {
            "111": {"resume_from": 9, "count": 1},
            "222": {"resume_from": 9, "count": 2},
        },
    }

    class _Route:
        key = "accountant"
        screen_enabled = False

    monkeypatch.setattr(ws.vacancies, "for_workua", lambda jid: _Route())

    logged: list[dict] = []
    monkeypatch.setattr(
        ws.log, "warning", lambda event, **kw: logged.append(kw) if "catch_up" in event else None
    )

    total = await ws.catch_up_skipped(client=client, router=router, cursor=cursor)

    per_job = {entry["job_id"]: entry["replayed"] for entry in logged}
    assert per_job == {111: 1, 222: 2}
    assert total == 3
    assert cursor["skipped_jobs"] == {}
