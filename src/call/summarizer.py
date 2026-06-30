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
  - qualified: true if candidate meets vacancy requirements AND wants to proceed
  - best_callback_time: ISO datetime string if a callback was requested, else null
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
            "best_callback_time": {"type": ["string", "null"]},
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
    best_callback_time: str | None = None
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
                    best_callback_time=d.get("best_callback_time"),
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
