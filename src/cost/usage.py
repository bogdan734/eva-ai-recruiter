"""Record Anthropic usage that happens outside a call.

Call tokens live on `Call.tokens_input/output` (written by the summarizer). Every
other prompt Єва runs — the name-origin gate on each intake, match scoring, the
Telegram userbot's classify/reply prompts — lands here, so `/costs` can add the
two together instead of quietly reporting only the summaries.

Recording must never break the thing it is measuring: every failure is logged and
swallowed. A missing token row costs us an accounting rounding error; an
exception here would cost us a candidate.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select

from src.common.db import session_scope
from src.common.models import TokenUsage

log = structlog.get_logger()

# Component keys — keep them short, they are grouped in /costs.
CALL_SUMMARY = "call_summary"   # already counted via Call.tokens_*; never recorded here
NAME_ORIGIN = "name_origin"
SCORER = "scorer"
TG_USERBOT = "tg_userbot"


async def record(
    component: str,
    usage: Any,
    *,
    model: str = "",
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> None:
    """Store one API response's token usage.

    `usage` is an Anthropic `resp.usage` object; pass None and give tokens_in/out
    explicitly when the numbers arrive over HTTP (the userbot does this).
    """
    try:
        tin = tokens_in if tokens_in is not None else int(getattr(usage, "input_tokens", 0) or 0)
        tout = tokens_out if tokens_out is not None else int(getattr(usage, "output_tokens", 0) or 0)
        if not tin and not tout:
            return
        async with session_scope() as session:
            session.add(
                TokenUsage(
                    date=datetime.utcnow().strftime("%Y-%m-%d"),
                    component=component[:32],
                    model=(model or "")[:64],
                    tokens_input=tin,
                    tokens_output=tout,
                )
            )
    except Exception as e:  # noqa: BLE001 — accounting must never break the pipeline
        log.warning("token_usage.record_failed", component=component, error=str(e))


async def tokens_since(since: datetime) -> tuple[int, int]:
    """(input, output) tokens recorded after `since`. Zero when the table is empty."""
    try:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(TokenUsage.tokens_input), 0),
                        func.coalesce(func.sum(TokenUsage.tokens_output), 0),
                    ).where(TokenUsage.created_at >= since)
                )
            ).one()
        return int(row[0] or 0), int(row[1] or 0)
    except Exception as e:  # noqa: BLE001 — an unmigrated DB must not break /costs
        log.warning("token_usage.read_failed", error=str(e))
        return 0, 0


async def tokens_for_date(day: str) -> tuple[int, int]:
    """(input, output) tokens recorded on a YYYY-MM-DD day."""
    try:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(TokenUsage.tokens_input), 0),
                        func.coalesce(func.sum(TokenUsage.tokens_output), 0),
                    ).where(TokenUsage.date == day)
                )
            ).one()
        return int(row[0] or 0), int(row[1] or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("token_usage.read_failed", error=str(e))
        return 0, 0


async def breakdown_since(since: datetime) -> list[dict]:
    """Per-component totals, biggest first — what /costs shows under Anthropic."""
    try:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(
                        TokenUsage.component,
                        func.coalesce(func.sum(TokenUsage.tokens_input), 0),
                        func.coalesce(func.sum(TokenUsage.tokens_output), 0),
                    )
                    .where(TokenUsage.created_at >= since)
                    .group_by(TokenUsage.component)
                )
            ).all()
        out = [
            {"component": r[0], "tokens_in": int(r[1] or 0), "tokens_out": int(r[2] or 0)}
            for r in rows
        ]
        out.sort(key=lambda d: d["tokens_in"] + d["tokens_out"], reverse=True)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("token_usage.read_failed", error=str(e))
        return []


async def prune(older_than_days: int = 120) -> int:
    """Drop ancient rows — this table grows one row per prompt."""
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    try:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(TokenUsage).where(TokenUsage.created_at < cutoff)
                )
            ).scalars().all()
            for r in rows:
                await session.delete(r)
            return len(rows)
    except Exception as e:  # noqa: BLE001
        log.warning("token_usage.prune_failed", error=str(e))
        return 0
