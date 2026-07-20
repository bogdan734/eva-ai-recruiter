"""Daily report aggregator + markdown formatter.

Reads DB rollups, formats the message exactly as discussed with the client.
Used by the TG bot at 09:00 Europe/Kyiv to send a digest for the previous day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select

from src.common.db import session_scope
from src.common.models import Call, CallStatus, Candidate, CandidateStatus
from src.cost.tracker import rollup_for_date


@dataclass
class DayReport:
    target_date: date
    attempts: int = 0
    success: int = 0
    no_answer: int = 0
    hangup: int = 0
    blocked: int = 0
    qualified: int = 0
    avg_success_sec: int = 0
    total_in_line_sec: int = 0
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    scraper_new: int = 0
    scraper_filtered: int = 0
    scraper_queued: int = 0
    funnel_to_call: int = 0
    funnel_calling: int = 0
    funnel_manager: int = 0
    funnel_archive_today: int = 0
    qualified_links: list[dict[str, str]] = field(default_factory=list)


def _fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "00:00"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


async def collect_for(target: date) -> DayReport:
    rep = DayReport(target_date=target)
    iso = target.isoformat()
    async with session_scope() as session:
        # Calls aggregation
        rows = await session.execute(
            select(Call.status, func.count(Call.id), func.coalesce(func.sum(Call.duration_sec), 0)).where(
                func.date(Call.started_at) == iso
            ).group_by(Call.status)
        )
        success_durations: list[int] = []
        for status, count, total_dur in rows.all():
            rep.attempts += count
            rep.total_in_line_sec += int(total_dur or 0)
            if status == CallStatus.SUCCESS:
                rep.success = count
                success_durations.append(int(total_dur or 0))
            elif status in (CallStatus.NO_ANSWER, CallStatus.BUSY, CallStatus.VOICEMAIL):
                rep.no_answer += count
            elif status == CallStatus.HANGUP:
                rep.hangup = count
            elif status == CallStatus.BLOCKED:
                rep.blocked = count

        if success_durations and rep.success:
            rep.avg_success_sec = int(sum(success_durations) / rep.success)

        # Qualified count (today)
        qualified_q = await session.execute(
            select(func.count(Candidate.id)).where(
                Candidate.status == CandidateStatus.MANAGER_REVIEW,
                func.date(Candidate.updated_at) == iso,
            )
        )
        rep.qualified = int(qualified_q.scalar() or 0)

        # Funnel snapshot
        snap = await session.execute(
            select(Candidate.status, func.count(Candidate.id)).group_by(Candidate.status)
        )
        for status, count in snap.all():
            if status == CandidateStatus.IN_CALL_QUEUE:
                rep.funnel_to_call = count
            elif status == CandidateStatus.CALLING:
                rep.funnel_calling = count
            elif status == CandidateStatus.MANAGER_REVIEW:
                rep.funnel_manager = count

    rep.cost_breakdown = await rollup_for_date(target)
    return rep


def format_report_md(rep: DayReport, keycrm_card_url: str = "") -> str:
    cb = rep.cost_breakdown
    total = cb.get("total_usd", 0.0)
    qualified_section = ""
    if rep.qualified_links:
        lines = [
            f"• {q['name']} ({q['score']}/100) — [картка KeyCRM ↗]({q['url']})"
            for q in rep.qualified_links
        ]
        qualified_section = "\n⭐ Кваліфіковані:\n" + "\n".join(lines) + "\n"

    pct = lambda x: f"{(x / rep.attempts * 100):.1f}%" if rep.attempts else "0.0%"

    return (
        f"📊 *Звіт за {rep.target_date.strftime('%d.%m.%Y')}*\n"
        f"\n"
        f"📞 *Дзвінки*\n"
        f"├ Спроб: {rep.attempts}\n"
        f"├ ✅ Успішні: {rep.success} ({pct(rep.success)})\n"
        f"├ ❌ Не відповідає: {rep.no_answer} ({pct(rep.no_answer)})\n"
        f"├ ⚠️ Скинули: {rep.hangup} ({pct(rep.hangup)})\n"
        f"├ 🚫 Guardrails: {rep.blocked}\n"
        f"└ ⭐ Кваліфіковано: {rep.qualified}\n"
        f"\n"
        f"⏱ *Час*\n"
        f"├ Сер. успішний: {_fmt_duration(rep.avg_success_sec)}\n"
        f"└ Всього в лінії: {_fmt_duration(rep.total_in_line_sec)}\n"
        f"\n"
        f"💰 *Витрати*\n"
        f"├ Claude: ${cb.get('claude_usd', 0):.2f}\n"
        f"├ Deepgram: ${cb.get('deepgram_usd', 0):.2f}\n"
        f"├ ElevenLabs: ${cb.get('elevenlabs_usd', 0):.2f}\n"
        f"├ Vapi: ${cb.get('vapi_usd', 0):.2f}\n"
        f"├ Telephony: ${cb.get('telephony_usd', 0):.2f}\n"
        f"└ *Total: ${total:.2f}*\n"
        f"   $/qualified: ${(total / rep.qualified) if rep.qualified else 0:.2f}\n"
        f"\n"
        f"🔍 *Скрапер*\n"
        f"├ Нових резюме: {rep.scraper_new}\n"
        f"├ Пройшли фільтр: {rep.scraper_filtered}\n"
        f"└ В черзі дзвінків: +{rep.scraper_queued}\n"
        f"\n"
        f"🎯 *Воронка*\n"
        f"├ To call: {rep.funnel_to_call}\n"
        f"├ Calling: {rep.funnel_calling}\n"
        f"├ → Менеджер: {rep.funnel_manager}\n"
        f"└ Архів сьогодні: {rep.funnel_archive_today}\n"
        f"{qualified_section}"
    )


async def yesterdays_report() -> str:
    target = date.today() - timedelta(days=1)
    rep = await collect_for(target)
    return format_report_md(rep)
