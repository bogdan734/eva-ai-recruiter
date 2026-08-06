"""Post-call summarizer.

Takes a transcript (Vapi end-of-call-report payload) and runs Claude Haiku to extract:
- 3-bullet summary in Ukrainian
- sentiment (positive/neutral/negative)
- objections raised (from a fixed enum)
- language used in the call (uk/ru/en/mixed)
- qualified flag (true if candidate fits and wants to proceed)
- best_callback_time if candidate requested one

Cheap (~$0.002/call). Result drops directly into KeyCRM custom fields.
"""
from __future__ import annotations

from dataclasses import dataclass

import anthropic
import structlog

from src.common.settings import get_settings

log = structlog.get_logger()

_SYSTEM = """You are a recruitment call analyzer.

You receive a transcript of an outbound recruiter call (AI agent → candidate).
You must return ONLY a tool call to summarize_call with these fields:
  - summary: 3 short bullet points (Ukrainian, <= 250 chars total)
  - sentiment: positive | neutral | negative
  - objections: array from [distance, salary, timing, field, current_job, other, none]
  - language: uk | ru | en | mixed
  - qualified: true ONLY if ALL of these are known AND fit: (a) at least ~1 year
    real work experience in sales/logistics/client work, (b) a confirmed city in
    one of these oblasts ONLY — Житомирська, Хмельницька, Тернопільська, Львівська, Івано-Франківська, Закарпатська, Чернівецька, Рівненська, Волинська, Черкаська, Одеська. Any other oblast does not fit, including
    Kyiv city AND the whole Kyiv oblast, and Vinnytsia oblast.
    Do NOT reason about "right bank" or geography — use this list literally,
    (c) a stated age. If region OR age was never given, or the call
    ended before both were collected, qualified MUST be false — an incomplete
    screening is not a qualified candidate.
  - candidate_age: the age in years the candidate stated during the call, else null
  - candidate_region: the city/town the candidate confirmed, else null
  - potentially_fit: true if the person showed RELEVANT experience (sales/logistics,
    ~1y+) and was serious, but the call ended before region AND age were both
    collected. Worth finishing in a message. False if they clearly do not fit or
    were a time-waster.
  - time_waster: true if the person was clearly not serious — trolling, absurd
    answers, mocking, refusing to answer while chatting. Such a call is a real
    conversation but the candidate does NOT fit (qualified=false).
  - connection_problem: true if the call connected but the two sides could not hear
    each other (candidate repeats 'алло', says they cannot hear, line noise/silence
    while both are present). False for a plain no-answer or voicemail.
  - spoke_with_candidate: true ONLY if the candidate ANSWERED AT LEAST ONE screening
    question about themselves (experience, city, age, current job or availability).
    Greeting the agent, confirming their name, or listening to the pitch is NOT enough.
    False for voicemail, silence, immediate hangup, wrong number, 'cannot hear you',
    and any call that ended before the candidate said anything about themselves.
  - needs_anketa: true if the agent promised to send an anketa/form link in Telegram
    (candidate was told the form would be sent), or the candidate agreed to fill a form
  - best_callback_time: ISO datetime string if a callback was requested, else null
  - reject_reason: WHY the candidate does not fit — one of:
      "not_target"     — off-portrait from the start: city NOT in the allowed oblast
                         list above (which excludes the city of Kyiv itself, and
                         Sumy, Zaporizhzhia, Kherson, Donetsk), age outside ~23-42,
                         or NO relevant sales/logistics experience at all (only
                         courses/studying). Anyone who never matched the target profile.
      "misbehaved"     — rude, abusive, trolling, mocking, insulting.
      "not_interested" — fits or plausibly fits, but declines: already found a job, not
                         interested, refuses full-time / wants to combine with another job.
      "none"           — qualified, or still in progress (no rejection decided).
    Pick the SINGLE best reason. If qualified=true, use "none".
"""

_TOOL = {
    "name": "summarize_call",
    "description": "Return structured summary of a recruitment call",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": 600},
            "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
            "objections": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "distance",
                        "salary",
                        "timing",
                        "field",
                        "current_job",
                        "other",
                        "none",
                    ],
                },
            },
            "language": {"type": "string", "enum": ["uk", "ru", "en", "mixed"]},
            "qualified": {"type": "boolean"},
            "candidate_age": {"type": ["integer", "null"]},
            "candidate_region": {"type": ["string", "null"]},
            "time_waster": {"type": "boolean"},
            "potentially_fit": {"type": "boolean"},
            "spoke_with_candidate": {"type": "boolean"},
            "connection_problem": {"type": "boolean"},
            "needs_anketa": {"type": "boolean"},
            "best_callback_time": {"type": ["string", "null"]},
            "reject_reason": {
                "type": "string",
                "enum": ["not_target", "misbehaved", "not_interested", "none"],
            },
        },
        "required": ["summary", "sentiment", "objections", "language", "qualified"],
    },
}


@dataclass
class CallSummary:
    summary: str
    sentiment: str
    objections: list[str]
    language: str
    qualified: bool
    needs_anketa: bool = False
    candidate_age: int | None = None
    candidate_region: str | None = None
    time_waster: bool = False
    potentially_fit: bool = False
    spoke_with_candidate: bool = False
    connection_problem: bool = False
    best_callback_time: str | None = None
    reject_reason: str = "none"
    tokens_in: int = 0
    tokens_out: int = 0


class Summarizer:
    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        s = get_settings()
        self._client = client or anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
        self._model = s.anthropic_model_cheap

    async def summarize(
        self,
        *,
        transcript: str,
        vacancy_title: str | None = None,
        vacancy_requirements: str | None = None,
    ) -> CallSummary:
        context = ""
        if vacancy_title:
            context += f"VACANCY: {vacancy_title}\n"
        if vacancy_requirements:
            context += f"REQUIREMENTS: {vacancy_requirements}\n"
        user_msg = f"{context}\nTRANSCRIPT:\n{transcript.strip()}\n\nUse the summarize_call tool."

        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=600,
            system=[
                {"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}
            ],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "summarize_call"},
            messages=[{"role": "user", "content": user_msg}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "summarize_call":
                d = block.input
                return CallSummary(
                    summary=str(d["summary"]),
                    sentiment=str(d["sentiment"]),
                    objections=list(d.get("objections", [])),
                    language=str(d["language"]),
                    qualified=bool(d["qualified"]),
                    needs_anketa=bool(d.get("needs_anketa", False)),
                    candidate_age=d.get("candidate_age"),
                    candidate_region=d.get("candidate_region"),
                    time_waster=bool(d.get("time_waster", False)),
                    potentially_fit=bool(d.get("potentially_fit", False)),
                    spoke_with_candidate=bool(d.get("spoke_with_candidate", False)),
                    connection_problem=bool(d.get("connection_problem", False)),
                    best_callback_time=d.get("best_callback_time"),
                    reject_reason=str(d.get("reject_reason", "none")),
                    tokens_in=resp.usage.input_tokens,
                    tokens_out=resp.usage.output_tokens,
                )
        log.warning("summarizer.tool_call_missing")
        return CallSummary(
            summary="",
            sentiment="neutral",
            objections=["none"],
            language="uk",
            qualified=False,
        )
