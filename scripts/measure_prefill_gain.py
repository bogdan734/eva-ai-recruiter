"""Does the shorter prompt actually make Eva answer sooner? Measure, don't guess.

Time-to-first-token is the part of the pause the prompt can move. Everything
else in the 1.7s the candidate hears -- endpointing, TTS start, the carrier --
is unaffected by prompt length, so this number is the honest ceiling on what
the rewrite buys.

Five runs each, alternating, so a slow moment on Anthropic's side does not land
entirely on one variant.
"""
import os
import statistics
import time

import anthropic

from src.call.script_template import render_system_prompt

import importlib.util
spec = importlib.util.spec_from_file_location(
    "proposal", "/app/scripts/prompt_compact_proposal.py")
proposal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proposal)

KEY = ""
for line in open(os.environ.get("ENV_FILE", "/app/.env")):
    if line.startswith("ANTHROPIC_API_KEY="):
        KEY = line.split("=", 1)[1].strip()
client = anthropic.Anthropic(api_key=KEY)

LONG = render_system_prompt(
    candidate_name="Тест Тестенко", candidate_phone="+380670000000",
    candidate_position="Менеджер з продажу", source="workua_response_send",
    company_pitch=None, vacancy_schedule=None, vacancy_benefits=None,
    vacancy_title="Менеджер з продажу логістики", vacancy_pitch="",
    vacancy_requirements="", vacancy_salary="обговорюється",
    vacancy_location="Україна",
)
SHORT = proposal.COMPACT.substitute(**proposal.SAMPLE)

# The turn that actually matters: the candidate has just said "так" to
# "Зручно говорити?" and is waiting. Cold prefill, shortest possible input.
TURN = [{"role": "user", "content": "Так"}]


def ttft(system: str) -> float:
    t0 = time.perf_counter()
    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=75,
        system=system, messages=TURN,
    ) as stream:
        for _ in stream.text_stream:
            return time.perf_counter() - t0
    return time.perf_counter() - t0


def count(system: str) -> int:
    return client.messages.count_tokens(
        model="claude-haiku-4-5-20251001", system=system, messages=TURN
    ).input_tokens


print(f"довгий промпт: {count(LONG)} токенів")
print(f"стислий промпт: {count(SHORT)} токенів")
print()

long_t, short_t = [], []
for i in range(5):
    long_t.append(ttft(LONG))
    short_t.append(ttft(SHORT))
    print(f"  прогін {i+1}: довгий {long_t[-1]:.3f}s | стислий {short_t[-1]:.3f}s")

print()
ml, ms = statistics.median(long_t), statistics.median(short_t)
print(f"медіана часу до першого токена:")
print(f"  довгий  {ml:.3f} s")
print(f"  стислий {ms:.3f} s")
print(f"  виграш  {ml - ms:+.3f} s  ({(ml - ms) / ml * 100:+.0f}%)")
print()
print("Нагадування: це лише частина паузи, яку чує кандидат (~1.7 s).")
print("Решта — endpointing, старт синтезу голосу і мережа, і вони від промпту не залежать.")
