"""Re-point three tests at the rule instead of at its old wording.

Each of these was written to protect something real -- never speak a
demographic reason, never reveal the geo filter, never introduce yourself twice
-- but each asserted on one exact English sentence. The compressed script says
all three things in fewer words, so the tests failed while the rules held.

Matching on a lowercased, whitespace-collapsed phrase keeps the protection and
stops a future reflow from failing the build for no reason. What must NOT be
relaxed is the presence check itself: if a rule genuinely disappears from the
prompt, these still go red.
"""
import pathlib

# ------------------------------------------------- tests/test_script_template.py
p = pathlib.Path("/app/tests/test_script_template.py")
t = p.read_text()

OLD_AGE = '''def test_age_discrimination_rule_present():
    out = render_system_prompt()
    assert "NEVER mention age" in out
    assert "discrimination" in out'''
NEW_AGE = '''def _flat(text: str) -> str:
    """Lowercased, single-spaced — so a line wrap is not a test failure."""
    return " ".join(text.split()).lower()


def test_age_discrimination_rule_present():
    """A candidate outside the age window is closed politely and never told why."""
    out = _flat(render_system_prompt())
    assert "never give a demographic reason" in out
    assert "never state the limits aloud" in out'''
assert t.count(OLD_AGE) == 1, "age test not found exactly once"
t = t.replace(OLD_AGE, NEW_AGE, 1)

OLD_GEO = '''def test_geo_reasons_are_never_spoken_to_the_candidate():
    out = render_system_prompt()
    assert "GEO IS INTERNAL LOGIC ONLY" in out'''
NEW_GEO = '''def test_geo_reasons_are_never_spoken_to_the_candidate():
    out = _flat(render_system_prompt())
    assert "geo is internal logic only" in out
    assert "never say a city fits or does not" in out'''
assert t.count(OLD_GEO) == 1, "geo test not found exactly once"
t = t.replace(OLD_GEO, NEW_GEO, 1)
p.write_text(t)
print("patched tests/test_script_template.py")

# ---------------------------------------------------- tests/test_call_opening.py
q = pathlib.Path("/app/tests/test_call_opening.py")
s = q.read_text()

OLD_INTRO = '''    p = _prompt()
    assert "NEVER introduce yourself again" in p
    # The line wraps in the template, so match on collapsed whitespace rather
    # than pinning the wrap point -- reflowing a paragraph is not a regression.
    flat = " ".join(p.split())
    assert "NEVER ask again whether it is convenient" in flat'''
NEW_INTRO = '''    # Collapsed and lowercased: the rule is what matters, not its wrapping.
    flat = " ".join(_prompt().split()).lower()
    assert "already spoken by the system" in flat
    assert "never greet, introduce yourself or ask about convenience again" in flat'''
assert s.count(OLD_INTRO) == 1, "intro test not found exactly once"
s = s.replace(OLD_INTRO, NEW_INTRO, 1)
q.write_text(s)
print("patched tests/test_call_opening.py")
