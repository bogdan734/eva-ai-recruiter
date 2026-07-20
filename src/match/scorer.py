"""Candidate-vacancy match scorer using Claude.

Returns a 0..1 score with a short rationale. Designed to be cheap (~$0.001/call):
- Haiku model
- Strict JSON output via tool-use
- Prompt cached system message (~500 tokens)
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import anthropic
import structlog

from src.common.settings import get_settings

log = structlog.get_logger()

_SYSTEM_PROMPT = """You are a recruitment matching evaluator.

Given a vacancy description and a candidate resume, score how well the candidate fits
on a scale 0.0 to 1.0:
  - 0.0 = no match (different field, no transferable skills)
  - 0.5 = partial match (some relevant skills, missing key requirements)
  - 0.7 = good match (most requirements met, ready for screening call)
  - 1.0 = excellent match (all requirements + bonus skills)

Return ONLY a JSON object via the score_match tool with:
  - score: float 0..1
  - rationale: short string (<= 200 chars), one sentence, in Ukrainian
  - missing: list of key vacancy requirements the candidate is missing
"""

_TOOL = {
    "name": "score_match",
    "description": "Return match score for candidate vs vacancy",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "rationale": {"type": "string", "maxLength": 200},
            "missing": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["score", "rationale", "missing"],
    },
}


@dataclass
class MatchResult:
    score: float
    rationale: str
    missing: list[str]


class MatchScorer:
    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        s = get_settings()
        self._client = client or anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
        self._model = s.anthropic_model_cheap

    async def score(self, vacancy_text: str, candidate_text: str) -> MatchResult:
        user_msg = (
            f"VACANCY:\n{vacancy_text.strip()}\n\n"
            f"CANDIDATE RESUME:\n{candidate_text.strip()}\n\n"
            "Use the score_match tool to return the result."
        )
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=400,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "score_match"},
            messages=[{"role": "user", "content": user_msg}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "score_match":
                data = block.input
                return MatchResult(
                    score=float(data.get("score", 0.0)),
                    rationale=str(data.get("rationale", ""))[:200],
                    missing=list(data.get("missing", [])),
                )
        log.warning("match.tool_call_missing", model=self._model)
        return MatchResult(score=0.0, rationale="no tool call", missing=[])


class FakeMatchScorer:
    """Deterministic stub for tests / dev — no API calls."""

    def __init__(self, score: float = 0.75) -> None:
        self.score_value = score

    async def score(self, vacancy_text: str, candidate_text: str) -> MatchResult:
        words_v = set(vacancy_text.lower().split())
        words_c = set(candidate_text.lower().split())
        overlap = len(words_v & words_c) / max(len(words_v), 1)
        return MatchResult(score=overlap, rationale="stub overlap match", missing=[])
