"""robota.ua applies the poller walked past and never came back for.

The poll reads a window of the newest applies and then moves its date cursor to
the newest one it saw. Anything in that window it did not finish is older than
the cursor on the next run and is never looked at again, unless it was parked.
Two kinds were not parked:

  * applies left over when the per-poll CV budget ran out. The code said
    "retry on the next poll"; the cursor made sure there was none. `Interaction`
    rows, rejected anyway, were spending that budget first.
  * applies to a posting the registry does not know yet: counted, warned about,
    and gone for good, even after someone mapped the id in the panel. work.ua
    has replayed those since August; robota.ua never did.

On 24.09 the recruiter saw two robota.ua applicants in the cabinet and neither
in the CRM.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.api.inbound_router import IngestResult
from src.integrations import robotaua_sync as rs

SALES_ID = 11277559
NEW_POSTING = 11399001


def _apply(apply_id: int, name: str, *, vacancy=SALES_ID, kind="Notepad", phone="+380671234567"):
    added = datetime.utcnow() - timedelta(minutes=apply_id % 50)
    return {
        "id": apply_id,
        "resumeId": apply_id + 1000,
        "name": name,
        "vacancyId": vacancy,
        "resumeType": kind,
        "phone": phone,
        "addDate": added.isoformat(timespec="seconds"),
    }


class _Cabinet:
    def __init__(self, applies):
        self.applies = applies
        self.resume_calls: list[int] = []

    async def city_map(self):
        return {}

    async def list_applies(self, *, page=0, count=50):
        return list(self.applies) if page == 0 else []

    async def get_resume(self, resume_id):
        self.resume_calls.append(resume_id)
        return {}

    async def open_contacts_count(self):
        return {"availableContacts": 0}

    async def download_attachment(self, apply_id, url=None):
        return b""


class _Router:
    def __init__(self):
        self.names: list[str] = []

    async def ingest(self, payload):
        self.names.append(payload.full_name)
        return IngestResult(accepted=True, candidate_id=len(self.names))


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOTAUA_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ROBOTAUA_ALLOWED_VACANCY_IDS", raising=False)
    monkeypatch.delenv("ROBOTAUA_DRY_RUN", raising=False)
    monkeypatch.setattr(rs, "REQUEST_PAUSE_SEC", 0)


@pytest.mark.asyncio
async def test_applies_left_over_by_the_cv_budget_come_in_on_the_next_poll(monkeypatch):
    cabinet = _Cabinet([_apply(3, "Катерина Шевченко"), _apply(2, "Інна Чудновська"),
                        _apply(1, "Третій Кандидат")])
    router = _Router()

    monkeypatch.setenv("ROBOTAUA_MAX_CV_FETCH", "1")
    await rs.poll_responses(client=cabinet, router=router)
    assert router.names == ["Катерина Шевченко"]
    assert set(rs.load_cursor()["pending"]) == {"2", "1"}

    monkeypatch.setenv("ROBOTAUA_MAX_CV_FETCH", "5")
    await rs.poll_responses(client=cabinet, router=router)
    assert sorted(router.names) == ["Інна Чудновська", "Катерина Шевченко", "Третій Кандидат"]
    assert rs.load_cursor()["pending"] == {}


@pytest.mark.asyncio
async def test_an_interaction_row_does_not_spend_the_cv_budget(monkeypatch):
    cabinet = _Cabinet([_apply(5, "Переглянув Вакансію", kind="Interaction"),
                        _apply(4, "Інна Чудновська")])
    router = _Router()
    monkeypatch.setenv("ROBOTAUA_MAX_CV_FETCH", "1")

    stats = await rs.poll_responses(client=cabinet, router=router)

    assert router.names == ["Інна Чудновська"]
    assert cabinet.resume_calls == [1004]
    assert stats.rejected == 1


@pytest.mark.asyncio
async def test_an_apply_to_an_unmapped_posting_is_replayed_once_it_is_mapped(monkeypatch):
    cabinet = _Cabinet([_apply(7, "Катерина Шевченко", vacancy=NEW_POSTING)])
    router = _Router()

    await rs.poll_responses(client=cabinet, router=router)
    assert router.names == []
    assert set(rs.load_cursor()["unmapped"]) == {"7"}

    # Someone adds the new posting's id (panel or env) — nothing else.
    monkeypatch.setenv("ROBOTAUA_ALLOWED_VACANCY_IDS", str(NEW_POSTING))
    await rs.poll_responses(client=cabinet, router=router)

    assert router.names == ["Катерина Шевченко"]
    cursor = rs.load_cursor()
    assert cursor["unmapped"] == {}
    assert cursor["pending"] == {}


def test_a_parked_entry_rebuilds_into_the_apply_it_came_from():
    """Budget-parked and released applies go through `_pending_as_apply` later."""
    apply = _apply(9, "Інна Чудновська", kind="AttachedFile") | {
        "fileName": "cv.pdf", "filePath": "https://apply-api.robota.ua/9-attach/file", "cityId": 4,
    }

    rebuilt = rs._pending_as_apply("9", rs._parked_entry(apply))

    for key in ("id", "resumeId", "name", "vacancyId", "resumeType", "fileName", "filePath", "cityId"):
        assert rebuilt[key] == apply[key]


@pytest.mark.asyncio
async def test_an_interaction_on_an_unmapped_posting_is_not_kept():
    """It would be refused on replay anyway; keeping it only grows the file."""
    cabinet = _Cabinet([_apply(8, "Переглянув Вакансію", vacancy=NEW_POSTING, kind="Interaction")])

    await rs.poll_responses(client=cabinet, router=_Router())

    assert rs.load_cursor().get("unmapped") == {}
