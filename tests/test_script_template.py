"""The call script Єва is given.

Rewritten 09.09.2026. The old assertions described a script two rewrites ago
and had been red for weeks: the company now renders in Cyrillic ("Козир Транс"
-- latin script gets pronounced in English by the TTS), the flow is STEP 0..9
rather than eleven steps, and the hardcoded blacklist of regions gave way to a
templated ${allowed_regions} plus explicit "never tell the candidate which
regions fit" wording, which is strictly better.

The recording-consent check below is deliberately kept as a strict xfail: it is
not stale, it is a real gap awaiting a business decision. See its docstring.
"""
import pytest

from src.call.script_template import render_system_prompt
from src.common.settings import get_settings


def test_renders_with_defaults():
    out = render_system_prompt()
    assert "Єва" in out
    # Cyrillic on purpose -- "Kozyr Trans" in latin letters is read out in
    # English by the TTS.
    assert "Козир Транс" in out
    # The vacancy title comes from settings, so read it from there rather than
    # pinning a string that moves whenever the posting is renamed.
    assert get_settings().default_vacancy_title in out
    # Salary and schedule are configuration too -- assert what is configured,
    # not a literal that changes with the posting.
    # Schedule is injected verbatim from settings. Salary deliberately is NOT:
    # it is spoken through a dedicated pay script with the numbers written as
    # words, because the TTS reads digits out as a string of separate digits.
    assert get_settings().default_vacancy_schedule in out
    assert "зарплата" in out.lower()


def test_includes_every_step_of_the_flow():
    out = render_system_prompt()
    for n in range(0, 10):
        assert f"STEP {n}" in out, f"missing STEP {n}"


def test_answering_machine_is_handled_before_anything_else():
    """STEP 0 exists so Єва hangs up on voicemail instead of pitching to it and
    burning a call attempt plus Vapi minutes."""
    out = render_system_prompt()
    assert "STEP 0" in out
    assert out.index("STEP 0") < out.index("STEP 1")


def _flat(text: str) -> str:
    """Lowercased, single-spaced — so a line wrap is not a test failure."""
    return " ".join(text.split()).lower()


def test_age_discrimination_rule_present():
    """A candidate outside the age window is closed politely and never told why."""
    out = _flat(render_system_prompt())
    assert "never give a demographic reason" in out
    assert "never state the limits aloud" in out


def test_allowed_regions_are_injected_not_hardcoded():
    """Geography comes from settings, so a change to REGION_WHITELIST reaches
    the call without a redeploy of the prompt."""
    out = render_system_prompt()
    allowed = get_settings().regions_allowed
    assert allowed, "no whitelist configured -- nothing to inject"
    assert any(r in out for r in allowed)


def test_geo_reasons_are_never_spoken_to_the_candidate():
    out = _flat(render_system_prompt())
    assert "geo is internal logic only" in out
    assert "never say a city fits or does not" in out


def test_data_deletion_promise_present():
    """Someone who asks to be removed must get a concrete answer, not a dodge."""
    out = render_system_prompt()
    assert "30 днів" in out
    assert "видал" in out.lower()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Settled, deliberate: the script does not tell the candidate the call is "
        "recorded. The business raised and closed this question already (09.09.2026 "
        "— «уже подымали этот вопрос»), so it is not an oversight and not a task. "
        "Kept as a marker only so that the leftover machinery around it "
        "(ExitReason.CONSENT_DENIED, FSM.consent_given «привітання + згода на запис») "
        "does not read like a bug to the next person, and so that adding a "
        "disclosure later flips this test green on purpose rather than by accident."
    ),
)
def test_consent_and_recording_phrase_present():
    out = render_system_prompt()
    assert "запис" in out.lower()


def test_renders_with_overrides():
    out = render_system_prompt(
        agent_name="Марія",
        company_name="ТОВ Тест",
        candidate_name="Іван Петренко",
        candidate_region="Львів",
    )
    assert "Марія" in out
    assert "ТОВ Тест" in out
    assert "Іван Петренко" in out
    assert "Львів" in out


def test_persona_anti_bot_rule():
    out = render_system_prompt()
    assert "NEVER admit to being AI" in out or "NEVER reveal" in out
