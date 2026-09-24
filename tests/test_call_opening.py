"""The opening of a call, pinned where it was measured.

Both halves of the 23.09 fix are easy to undo by accident: the script is edited
by hand, and the override block looks redundant next to the assistant config it
duplicates. Each of these asserts one thing that cost a real conversation.
"""
import inspect

from src.call import orchestrator
from src.call.script_template import render_system_prompt


def _prompt() -> str:
    return render_system_prompt(
        candidate_name="Тест", candidate_phone="+380670000000",
        candidate_position="", source="workua_response_send",
        company_pitch=None, vacancy_schedule=None, vacancy_benefits=None,
        vacancy_title="Менеджер з продажу", vacancy_pitch="",
        vacancy_requirements="", vacancy_salary="обговорюється",
        vacancy_location="Україна",
    )


def test_eva_is_told_the_introduction_was_already_spoken():
    # Collapsed and lowercased: the rule is what matters, not its wrapping.
    flat = " ".join(_prompt().split()).lower()
    assert "already spoken by the system" in flat
    assert "never greet, introduce yourself or ask about convenience again" in flat


def test_the_offer_no_longer_carries_the_whole_pitch_in_one_breath():
    p = _prompt()
    assert "повністю віддалено." in p
    assert "дохід від тридцяти до шістдесяти п'яти тисяч" not in p, (
        "salary is an answer to a question, not part of the opening line"
    )


def test_the_override_repeats_maxtokens():
    """Vapi replaces the model object; an inherited cap is not a thing."""
    src = inspect.getsource(orchestrator)
    i = src.find('"model": {')
    assert i != -1
    block = src[i:i + 700]
    assert '"maxTokens"' in block, (
        "assistantOverrides must carry maxTokens or Vapi's default 250 applies"
    )
