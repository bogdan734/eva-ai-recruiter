"""The outreach must not write to people a recruiter has already decided about.

On 25.09 a recruiter moved Третьяк Руслана to «Не ЦА»; on 26.09 the daily walker
sent her "we never reached you — write here". Our own status still said
`manager_review`, so only the live KeyCRM stage can tell. A deleted card is a
decision too.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import respx

from src.common.settings import get_settings
from src.integrations import tg_outreach
from src.integrations.tg_outreach import run_once, skip_reason


class _CRM:
    def __init__(self, stages):
        self.stages = stages
        self.asked: list[int] = []

    async def live_card_status(self, lead_id):
        self.asked.append(lead_id)
        stage = self.stages[lead_id]
        if isinstance(stage, Exception):
            raise stage
        return stage

    async def aclose(self):
        pass


async def test_card_the_recruiter_rejected_is_skipped():
    why = await skip_reason(_CRM({11577: 34}), status="manager_review",
                            callback_at=None, lead_id=11577)
    assert why == "crm_stage:34"


async def test_deleted_card_is_skipped():
    why = await skip_reason(_CRM({10545: None}), status="manager_review",
                            callback_at=None, lead_id=10545)
    assert why == "card_deleted"


async def test_card_the_recruiter_took_is_skipped():
    why = await skip_reason(_CRM({7: 3}), status="manager_review", callback_at=None, lead_id=7)
    assert why == "crm_stage:3"


async def test_fresh_applicant_in_a_new_card_is_messaged():
    why = await skip_reason(_CRM({7: 1}), status="manager_review", callback_at=None, lead_id=7)
    assert why is None


async def test_decided_status_needs_no_crm_call():
    crm = _CRM({})
    why = await skip_reason(crm, status="closed", callback_at=None, lead_id=7)
    assert why == "status:closed"
    assert crm.asked == []


def _patch_queue(monkeypatch, people, facts):
    marked: list[int] = []

    async def pending(vacancy, created_after, limit):
        return people

    async def card_facts(ids):
        return facts

    async def mark(cid):
        marked.append(cid)

    monkeypatch.setattr(tg_outreach, "pending_candidates", pending)
    monkeypatch.setattr(tg_outreach, "_card_facts", card_facts)
    monkeypatch.setattr(tg_outreach, "_mark_sent", mark)
    return marked


@respx.mock
async def test_run_writes_only_to_live_cards_and_closes_the_rest(monkeypatch):
    people = [(1, "Анна", "+380500000001"), (2, "Руслана", "+380500000002"),
              (3, "Микита", "+380500000003")]
    facts = {1: ("manager_review", None, 11), 2: ("manager_review", None, 22),
             3: ("manager_review", None, 33)}
    marked = _patch_queue(monkeypatch, people, facts)
    route = respx.post(f"{get_settings().tguserbot_url}/send_outreach").mock(
        return_value=httpx.Response(200, json={"ok": True}))

    stats = await run_once(created_after=datetime(2026, 8, 18, tzinfo=UTC), send=True,
                           crm=_CRM({11: 1, 22: 34, 33: None}))

    assert route.call_count == 1
    assert b"+380500000001" in route.calls[0].request.content
    assert sorted(marked) == [1, 2, 3]
    assert (stats.sent, stats.skipped) == (1, 2)


@respx.mock
async def test_crm_outage_leaves_everyone_pending(monkeypatch):
    marked = _patch_queue(monkeypatch, [(1, "Анна", "+380500000001")],
                          {1: ("manager_review", None, 11)})
    route = respx.post(f"{get_settings().tguserbot_url}/send_outreach").mock(
        return_value=httpx.Response(200, json={"ok": True}))

    stats = await run_once(created_after=datetime(2026, 8, 18, tzinfo=UTC), send=True,
                           crm=_CRM({11: RuntimeError("keycrm 503")}))

    assert route.call_count == 0
    assert marked == []
    assert stats.stopped_on.startswith("keycrm")
