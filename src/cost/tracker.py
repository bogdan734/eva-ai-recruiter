"""Daily cost aggregator — reads call rows, rolls into DailyCost table."""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.common.db import session_scope
from src.common.models import Call, DailyCost

from .pricing import PRICING


async def rollup_for_date(target: date) -> dict[str, Any]:
    iso = target.isoformat()
    async with session_scope() as session:
        q = select(
            func.sum(Call.duration_sec),
            func.sum(Call.tokens_input),
            func.sum(Call.tokens_output),
            func.sum(Call.cost_usd),
            func.count(Call.id),
        ).where(func.date(Call.started_at) == iso)
        row = (await session.execute(q)).one()
        duration_sec, tok_in, tok_out, total_usd, count = row
        duration_min = (duration_sec or 0) / 60.0
        agg = {
            "date": iso,
            "calls": count or 0,
            "minutes": round(duration_min, 2),
            "tokens_in": tok_in or 0,
            "tokens_out": tok_out or 0,
            "total_usd": round(total_usd or 0.0, 4),
            "claude_usd": round(
                ((tok_in or 0) / 1_000_000) * PRICING.claude_in_per_mtok
                + ((tok_out or 0) / 1_000_000) * PRICING.claude_out_per_mtok,
                4,
            ),
            "deepgram_usd": round(duration_min * PRICING.deepgram_per_min, 4),
            "elevenlabs_usd": round(duration_min * PRICING.elevenlabs_per_min, 4),
            "vapi_usd": round(duration_min * PRICING.vapi_per_min, 4),
            "telephony_usd": round(duration_min * PRICING.twilio_per_min, 4),
        }
        stmt = sqlite_insert(DailyCost).values(
            date=iso,
            claude_tokens_in=agg["tokens_in"],
            claude_tokens_out=agg["tokens_out"],
            claude_usd=agg["claude_usd"],
            deepgram_minutes=agg["minutes"],
            deepgram_usd=agg["deepgram_usd"],
            elevenlabs_usd=agg["elevenlabs_usd"],
            vapi_minutes=agg["minutes"],
            vapi_usd=agg["vapi_usd"],
            telephony_minutes=agg["minutes"],
            telephony_usd=agg["telephony_usd"],
            total_usd=agg["total_usd"],
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["date"],
            set_=dict(
                claude_tokens_in=agg["tokens_in"],
                claude_tokens_out=agg["tokens_out"],
                claude_usd=agg["claude_usd"],
                deepgram_minutes=agg["minutes"],
                deepgram_usd=agg["deepgram_usd"],
                elevenlabs_usd=agg["elevenlabs_usd"],
                vapi_minutes=agg["minutes"],
                vapi_usd=agg["vapi_usd"],
                telephony_minutes=agg["minutes"],
                telephony_usd=agg["telephony_usd"],
                total_usd=agg["total_usd"],
            ),
        )
        await session.execute(stmt)
        return agg
