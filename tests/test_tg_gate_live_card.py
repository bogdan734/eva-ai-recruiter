"""Eva answers in Telegram unless a recruiter really has the card.

Every job-board applicant is `manager_review` from the moment they apply, so the
old status-based gate silenced Eva for all of them. The owner's rule (28.09):
Eva leads the dialog to the end; the live card decides who owns the person.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.api.tg_gate import decide


class _CRM:
    def __init__(self, stage=None, error=None):
        self.stage, self.error, self.asked = stage, error, 0

    async def live_card_status(self, lead_id):
        self.asked += 1
        if self.error:
            raise self.error
        return self.stage


def _cand(status="manager_review", lead=11577, callback_at=None):
    return SimpleNamespace(status=status, keycrm_lead_id=lead, callback_at=callback_at)


async def test_unknown_person_gets_an_answer():
    d = await decide(None, _CRM())
    assert (d.engage, d.notify) == (True, False)


async def test_applicant_whose_card_was_deleted_gets_an_answer():
    d = await decide(_cand(), _CRM(stage=None))
    assert (d.engage, d.notify, d.why) == (True, False, "card_deleted")


async def test_applicant_in_new_or_selected_gets_an_answer():
    for stage in (1, 2, 31):
        d = await decide(_cand(), _CRM(stage=stage))
        assert d.engage, stage


async def test_card_in_work_with_a_recruiter_keeps_eva_quiet_and_tells_them():
    d = await decide(_cand(), _CRM(stage=3))
    assert (d.engage, d.notify, d.why) == (False, True, "recruiter_stage")


async def test_rejected_card_keeps_eva_quiet_without_pinging_anyone():
    d = await decide(_cand(), _CRM(stage=34))
    assert (d.engage, d.notify, d.why) == (False, False, "final_stage")


async def test_closed_candidate_needs_no_crm_call():
    crm = _CRM(stage=2)
    d = await decide(_cand(status="closed"), crm)
    assert (d.engage, d.notify) == (False, False)
    assert crm.asked == 0


async def test_evas_own_statuses_need_no_crm_call():
    crm = _CRM(stage=3)
    d = await decide(_cand(status="call_done"), crm)
    assert d.engage
    assert crm.asked == 0


async def test_crm_outage_falls_back_to_quiet_and_tells_the_recruiter():
    d = await decide(_cand(), _CRM(error=RuntimeError("keycrm 503")))
    assert (d.engage, d.notify) == (False, True)
