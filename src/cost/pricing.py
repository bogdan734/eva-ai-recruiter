"""Per-service pricing (USD). Update when vendors change rates."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pricing:
    # Claude Sonnet 4.6 — $3/MTok in, $15/MTok out
    claude_in_per_mtok: float = 3.0
    claude_out_per_mtok: float = 15.0
    # Claude Haiku 4.5 — $1/MTok in, $5/MTok out
    haiku_in_per_mtok: float = 1.0
    haiku_out_per_mtok: float = 5.0
    # Deepgram Nova-3 streaming — $0.0043/minute
    deepgram_per_min: float = 0.0043
    # ElevenLabs Flash v2.5 — ~$0.05/min equivalent (Starter plan amortized)
    elevenlabs_per_min: float = 0.05
    # Vapi orchestration — $0.05/min
    vapi_per_min: float = 0.05
    # Twilio +380 outbound — ~$0.04/min average
    twilio_per_min: float = 0.04


PRICING = Pricing()


def claude_cost(tokens_in: int, tokens_out: int, cheap: bool = False) -> float:
    p = PRICING
    if cheap:
        return (tokens_in / 1_000_000) * p.haiku_in_per_mtok + (
            tokens_out / 1_000_000
        ) * p.haiku_out_per_mtok
    return (tokens_in / 1_000_000) * p.claude_in_per_mtok + (
        tokens_out / 1_000_000
    ) * p.claude_out_per_mtok


def call_cost(
    duration_min: float,
    tokens_in: int,
    tokens_out: int,
    telephony_per_min: float | None = None,
) -> dict[str, float]:
    p = PRICING
    tp = telephony_per_min if telephony_per_min is not None else p.twilio_per_min
    breakdown = {
        "claude": claude_cost(tokens_in, tokens_out),
        "deepgram": duration_min * p.deepgram_per_min,
        "elevenlabs": duration_min * p.elevenlabs_per_min,
        "vapi": duration_min * p.vapi_per_min,
        "telephony": duration_min * tp,
    }
    breakdown["total"] = round(sum(breakdown.values()), 4)
    return {k: round(v, 4) for k, v in breakdown.items()}
