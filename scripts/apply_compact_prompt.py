"""Swap the call script for the compressed one. Reviewed 23.09.2026.

Not a speed fix, and it is worth writing that down so nobody re-derives the
wrong reason later: measured on 5 alternating live API calls, time-to-first-
token was 0.576s on the 7 282-token script and 0.590s on the 4 020-token one.
Haiku does not care. The pause the candidate hears lives in endpointing and in
the start of speech synthesis, not in prefill.

What this buys: ~45% fewer input tokens on every turn of every call, and one
prompt that says each thing once. It also removes a defect introduced earlier
today -- the STEP 2 rewrite left the previous version's bullets dangling
underneath it, so the model was reading two overlapping sets of instructions
for the same step.

Every line Eva speaks aloud, and every rule that decides whether a candidate
continues, is carried over verbatim; scripts/prompt_compact_proposal.py checks
18 of them by substring and this patch refuses to run if any is missing.
"""
import importlib.util
import pathlib
import re
import sys

spec = importlib.util.spec_from_file_location(
    "proposal", "/app/scripts/prompt_compact_proposal.py")
proposal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proposal)

body = proposal.COMPACT.template.strip("\n")

# The tool-call instruction lives at the very end of the shipped template and is
# functional, not prose -- the orchestrator reads the steps it records.
TAIL = "\nAfter EVERY step, call update_call_state(step=N, ...) to record progress.\n"
body = body + "\n" + TAIL.strip("\n") + "\n"

rendered = proposal.COMPACT.substitute(**proposal.SAMPLE)
missing = [m for m in proposal.MUST_SURVIVE if m not in rendered]
if missing:
    print("ВІДМОВА: у стислому промпті бракує:", missing)
    sys.exit(1)
print("контрольні фрагменти:", len(proposal.MUST_SURVIVE), "— усі на місці")

p = pathlib.Path("/app/src/call/script_template.py")
t = p.read_text()

START = '_TPL = Template(\n    """\n'
END = '\n""".strip()\n)'
i = t.find(START)
j = t.find(END, i)
assert i != -1 and j != -1, "template literal boundaries not found"

old_body = t[i + len(START):j]
print("старий шаблон:", len(old_body), "символів")
print("новий шаблон :", len(body), "символів")

t = t[:i + len(START)] + body + t[j:]
p.write_text(t)
print("script_template.py оновлено")

# Placeholders the new template needs must all be produced by the renderer, or
# substitute() raises at call time -- i.e. on a live call, with the candidate
# already on the line.
needed = set(re.findall(r"\$\{(\w+)\}", body))
print("плейсхолдери в новому шаблоні:", sorted(needed))
