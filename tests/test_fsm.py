from src.call.fsm import CallState, Step


def test_fsm_starts_at_greeting():
    s = CallState()
    assert s.step == Step.GREETING
    assert int(s.step) == 1


def test_fsm_has_11_steps_kozyr_trans_flow():
    assert int(Step.GREETING) == 1
    assert int(Step.CONFIRM_INTENT) == 2
    assert int(Step.REGION_CHECK) == 3
    assert int(Step.PITCH) == 4
    assert int(Step.BEHAVIORAL) == 5
    assert int(Step.MOTIVATION) == 6
    assert int(Step.SALARY) == 7
    assert int(Step.SCHEDULE) == 8
    assert int(Step.TECH) == 9
    assert int(Step.INTEREST) == 10
    assert int(Step.HANDOFF) == 11


def test_fsm_advances_through_all_steps():
    s = CallState()
    seen = [s.step]
    while s.step < Step.HANDOFF:
        s.advance()
        seen.append(s.step)
    assert len(seen) == 11
    assert seen[0] == Step.GREETING
    assert seen[-1] == Step.HANDOFF


def test_fsm_does_not_advance_past_handoff():
    s = CallState(step=Step.HANDOFF)
    s.advance()
    assert s.step == Step.HANDOFF


def test_fsm_to_dict_includes_kozyr_fields():
    s = CallState(
        step=Step.SALARY,
        language="ru",
        consent_given=True,
        sales_type="phone",
        sales_experience_years=3,
        candidate_salary_expectation="30-40k",
        salary_script_delivered=True,
    )
    d = s.to_dict()
    assert d["step"] == int(Step.SALARY)
    assert d["language"] == "ru"
    assert d["consent_given"] is True
    assert d["sales_type"] == "phone"
    assert d["sales_experience_years"] == 3
    assert d["candidate_salary_expectation"] == "30-40k"
    assert d["salary_script_delivered"] is True
