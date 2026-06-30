"""Tests for the post-call summarizer using a mocked Anthropic client."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.call.summarizer import Summarizer


@dataclass
class _FakeBlock:
    type: str
    name: str | None = None
    input: dict[str, Any] | None = None


@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class _FakeResp:
    content: list[_FakeBlock]
    usage: _FakeUsage


class _FakeMessages:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    async def create(self, **kwargs: Any) -> _FakeResp:  # noqa: D401
        return self._resp


class _FakeAnthropic:
    def __init__(self, resp: _FakeResp) -> None:
        self.messages = _FakeMessages(resp)


@pytest.mark.asyncio
async def test_summarizer_parses_tool_call() -> None:
    fake_resp = _FakeResp(
        content=[
            _FakeBlock(
                type="tool_use",
                name="summarize_call",
                input={
                    "summary": "• Має 3 роки досвіду\n• Готовий розпочати з понеділка\n• Очікує 25к",
                    "sentiment": "positive",
                    "objections": ["salary"],
                    "language": "uk",
                    "qualified": True,
                    "best_callback_time": "2026-06-22T10:00:00",
                },
            )
        ],
        usage=_FakeUsage(input_tokens=512, output_tokens=128),
    )
    s = Summarizer(client=_FakeAnthropic(fake_resp))
    result = await s.summarize(transcript="...")
    assert result.qualified is True
    assert result.sentiment == "positive"
    assert "salary" in result.objections
    assert result.language == "uk"
    assert result.tokens_in == 512
    assert result.tokens_out == 128


@pytest.mark.asyncio
async def test_summarizer_fallback_on_missing_tool_call() -> None:
    fake_resp = _FakeResp(
        content=[_FakeBlock(type="text", input=None)],
        usage=_FakeUsage(),
    )
    s = Summarizer(client=_FakeAnthropic(fake_resp))
    result = await s.summarize(transcript="...")
    assert result.qualified is False
    assert result.sentiment == "neutral"
