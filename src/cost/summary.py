"""What the campaign is costing, computed from the calls we actually made.

No vendor exposes a balance to us: Vapi's private key is rejected by /org (that
needs the org key) and Anthropic has no balance endpoint outside the Admin API.
So the client tops up and tells the bot the new balance (`/set_balance`), and we
subtract our own metered spend from that moment on. Everything here is an
ESTIMATE from `src/cost/pricing.py` rates — good enough to answer "скільки
лишилось і на скільки вистачить", not an invoice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select

from src.common.db import session_scope
from src.common.models import Call

from . import usage
from .pricing import PRICING, claude_cost


@dataclass
class SpendReport:
    since: datetime
    calls: int = 0
    minutes: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    # Tokens spent outside calls (intake name check, scoring, Telegram userbot).
    # Kept separate so /costs can show where the Anthropic bill actually goes.
    off_call_tokens_in: int = 0
    off_call_tokens_out: int = 0
    per_service: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return round(sum(self.per_service.values()), 2)


async def spend_since(since: datetime, *, cheap_model: bool = True) -> SpendReport:
    """Metered spend for every call started after `since`."""
    async with session_scope() as session:
        row = (
            await session.execute(
                select(
                    func.count(Call.id),
                    func.coalesce(func.sum(Call.duration_sec), 0),
                    func.coalesce(func.sum(Call.tokens_input), 0),
                    func.coalesce(func.sum(Call.tokens_output), 0),
                ).where(Call.started_at >= since)
            )
        ).one()

    calls, duration_sec, tok_in, tok_out = row
    minutes = round((duration_sec or 0) / 60.0, 1)

    # Everything Єва thinks with between calls: the name-origin gate on every
    # intake, match scoring, the Telegram userbot. Before this was counted, the
    # Anthropic line only ever showed the post-call summaries.
    off_in, off_out = await usage.tokens_since(since)

    report = SpendReport(
        since=since,
        calls=calls or 0,
        minutes=minutes,
        tokens_in=tok_in or 0,
        tokens_out=tok_out or 0,
        off_call_tokens_in=off_in,
        off_call_tokens_out=off_out,
    )
    report.per_service = {
        # Vapi bills orchestration + the SIP leg together on this setup.
        "vapi": round(minutes * (PRICING.vapi_per_min + PRICING.twilio_per_min), 2),
        "anthropic": round(
            claude_cost(
                (tok_in or 0) + off_in, (tok_out or 0) + off_out, cheap=cheap_model
            ),
            2,
        ),
        "deepgram": round(minutes * PRICING.deepgram_per_min, 2),
    }
    return report


async def burn_rate(days: int = 7) -> float:
    """Average USD per day over the window — used for "вистачить на N днів"."""
    report = await spend_since(datetime.utcnow() - timedelta(days=days))
    return round(report.total / max(days, 1), 2)


async def balance_forecast(balances: dict) -> list[dict]:
    """For each balance the client entered: spent since, what is left, days left.

    `balances` shape: {"vapi": {"usd": 20.0, "at": "2026-08-04T09:00:00"}, ...}
    """
    daily = await burn_rate(7)
    out: list[dict] = []
    for service, entry in (balances or {}).items():
        try:
            topped_up = float(entry.get("usd") or 0)
            at = datetime.fromisoformat(str(entry.get("at"))[:19])
        except (TypeError, ValueError):
            continue
        report = await spend_since(at)
        spent = report.per_service.get(service, report.total if service == "all" else 0.0)
        left = round(topped_up - spent, 2)
        # Per-service share of the daily burn, so the forecast is not distorted
        # by the services this balance does not pay for.
        share = (
            report.per_service.get(service, 0.0) / report.total if report.total else 0.0
        )
        per_day = round(daily * share, 2) if share else 0.0
        out.append(
            {
                "service": service,
                "topped_up": round(topped_up, 2),
                "at": at.strftime("%d.%m %H:%M"),
                "spent": round(spent, 2),
                "left": left,
                "per_day": per_day,
                "days_left": round(left / per_day, 1) if per_day > 0 and left > 0 else None,
            }
        )
    return out
