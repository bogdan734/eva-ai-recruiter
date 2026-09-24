"""The call gate and the intake gate must use the same region list.

17.09.2026: they did not. REGION_WHITELIST allowed Дніпропетровська, the
analyzer prompt (hardcoded) did not, and a qualified Дніпро candidate was
called and then rejected as «Не ЦА».
"""
from src.call.summarizer import _system_prompt
from src.common.settings import get_settings


def test_prompt_lists_every_allowed_region_from_settings():
    prompt = _system_prompt()
    allowed = get_settings().regions_allowed
    assert allowed, "no whitelist configured"
    missing = [r for r in allowed if r not in prompt]
    assert not missing, f"allowed regions missing from the call prompt: {missing}"


def test_prompt_lists_the_blocked_regions_too():
    prompt = _system_prompt()
    blocked = get_settings().regions_blocked
    missing = [r for r in blocked if r not in prompt]
    assert not missing, f"blocked regions missing from the call prompt: {missing}"


def test_placeholders_are_actually_substituted():
    prompt = _system_prompt()
    assert "__ALLOWED_REGIONS__" not in prompt
    assert "__BLOCKED_REGIONS__" not in prompt


def test_dnipro_is_treated_as_allowed_wherever_settings_say_so():
    """The specific drift that cost a real candidate: if the environment allows
    Дніпропетровська, the person judging the call has to know that."""
    s = get_settings()
    if "Дніпропетровська" in s.regions_allowed:
        assert "Дніпропетровська" in _system_prompt()
