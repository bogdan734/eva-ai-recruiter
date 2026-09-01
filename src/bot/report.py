"""Daily report aggregator + markdown formatter.

Rewritten 2026-07-21: counts PEOPLE rather than attempts, shows every call
outcome (120 "failed" calls used to vanish from the report entirely), computes
real spend from tokens and minutes, breaks intake down by source, and includes
Telegram outreach.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select

from src.common.db import session_scope
from src.common.models import Call, CallStatus, Candidate, CandidateStatus
from src.common import sources as _sources
from src.cost.pricing import PRICING

# how a raw source string maps onto something a human wants to read
SOURCE_LABELS: dict[str, str] = {
    "workua": "work.ua",
    "workua_response_send": "work.ua",
    "workua_api": "work.ua",
    "robota": "robota.ua",
    "robotaua": "robota.ua",
    "inbound_call": "вхідні дзвінки",
    "manual": "ручний імпорт",
    "tg_test_call": "тестові",
}


def source_label(raw: str | None) -> str:
    return _sources.label(raw)


@dataclass
class DayReport:
    target_date: date
    # people, not attempts
    people_dialed: int = 0
    people_talked: int = 0
    qualified: int = 0
    rejected: int = 0
    dropped_early: int = 0
    unreachable: int = 0
    not_connected: int = 0
    attempts: int = 0
    avg_talk_sec: int = 0
    total_in_line_sec: int = 0
    cost: dict[str, float] = field(default_factory=dict)
    intake_by_source: dict[str, int] = field(default_factory=dict)
    tg_sent_today: int = 0
    tg_limit: int = 0
    tg_active: bool = False
    funnel_to_call: int = 0
    funnel_calling: int = 0
    funnel_manager: int = 0
    funnel_rejected: int = 0
    funnel_unreachable: int = 0
    qualified_names: list[str] = field(default_factory=list)
    robotaua_block: str = ""
    workua_postings_block: str = ""
    balances_block: str = ""


def _fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "00:00"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


async def _tg_stats() -> tuple[int, int, bool]:
    """Live Telegram outreach numbers; never let this break the report."""
    import json
    import urllib.request

    url = os.getenv("TGUSERBOT_URL", "http://tguserbot:8090")
    try:
        d = json.load(urllib.request.urlopen(f"{url}/stats", timeout=8))
        return int(d.get("sent_today", 0)), int(d.get("limit", 0)), bool(d.get("active"))
    except Exception:
        return 0, 0, False


TALK_FLOOR_SEC = 60  # below this nobody actually answered the screening questions


async def collect_for(target: date) -> DayReport:
    rep = DayReport(target_date=target)

    async with session_scope() as session:
        # ---- calls of the day, aggregated per person ----
        rows = (await session.execute(
            select(
                Call.candidate_id,
                func.count(Call.id),
                func.max(Call.duration_sec),
                func.sum(Call.duration_sec),
                func.sum(Call.tokens_input),
                func.sum(Call.tokens_output),
                func.max(Call.status),
            )
            .where(func.date(Call.started_at) == target)
            .group_by(Call.candidate_id)
        )).all()

        tokens_in = tokens_out = 0
        talk_secs: list[int] = []
        for _cid, n_calls, longest, total_sec, t_in, t_out, _st in rows:
            rep.attempts += int(n_calls or 0)
            rep.people_dialed += 1
            rep.total_in_line_sec += int(total_sec or 0)
            tokens_in += int(t_in or 0)
            tokens_out += int(t_out or 0)
            if (longest or 0) >= TALK_FLOOR_SEC:
                rep.people_talked += 1
                talk_secs.append(int(longest))
            elif (longest or 0) > 0:
                rep.dropped_early += 1
            else:
                rep.not_connected += 1

        if talk_secs:
            rep.avg_talk_sec = sum(talk_secs) // len(talk_secs)

        # ---- outcomes among the people called that day ----
        called_ids = [r[0] for r in rows]
        if called_ids:
            outcome = (await session.execute(
                select(Candidate.status, func.count(Candidate.id))
                .where(Candidate.id.in_(called_ids))
                .group_by(Candidate.status)
            )).all()
            for status, n in outcome:
                if status == CandidateStatus.MANAGER_REVIEW:
                    rep.qualified = n
                elif status == CandidateStatus.CLOSED:
                    rep.rejected = n
                elif status == CandidateStatus.UNREACHABLE:
                    rep.unreachable = n

            names = (await session.execute(
                select(Candidate.full_name)
                .where(Candidate.id.in_(called_ids),
                       Candidate.status == CandidateStatus.MANAGER_REVIEW)
                .order_by(Candidate.full_name)
            )).scalars().all()
            rep.qualified_names = list(names)

        # ---- intake of the day, split by source ----
        intake = (await session.execute(
            select(Candidate.source, func.count(Candidate.id))
            .where(func.date(Candidate.created_at) == target)
            .group_by(Candidate.source)
        )).all()
        for raw, n in intake:
            label = source_label(raw)
            rep.intake_by_source[label] = rep.intake_by_source.get(label, 0) + n

        # ---- funnel snapshot (current, not per-day) ----
        funnel = dict((s, n) for s, n in (await session.execute(
            select(Candidate.status, func.count(Candidate.id)).group_by(Candidate.status)
        )).all())
        g = lambda st: funnel.get(st, 0)
        rep.funnel_to_call = g(CandidateStatus.IN_CALL_QUEUE) + g(CandidateStatus.NEW_RESUME)
        rep.funnel_calling = g(CandidateStatus.CALLING)
        rep.funnel_manager = g(CandidateStatus.MANAGER_REVIEW)
        rep.funnel_rejected = g(CandidateStatus.CLOSED)
        rep.funnel_unreachable = g(CandidateStatus.UNREACHABLE)

        # People dialled today who are still queued for another attempt — they
        # were not lost, they carried over to the next day.
        rep.carried_over = (await session.execute(
            select(func.count(func.distinct(Candidate.id)))
            .select_from(Candidate)
            .join(Call, Call.candidate_id == Candidate.id)
            .where(
                func.date(Call.started_at) == target,
                Candidate.status.in_((
                    CandidateStatus.IN_CALL_QUEUE,
                    CandidateStatus.NEW_RESUME,
                )),
            )
        )).scalar() or 0

    # ---- spend, computed from what actually happened ----
    minutes = rep.total_in_line_sec / 60
    p = PRICING
    claude = (tokens_in / 1_000_000) * p.haiku_in_per_mtok + (
        tokens_out / 1_000_000) * p.haiku_out_per_mtok
    # Vapi + Anthropic only: Vapi's per-minute price already covers STT/TTS, and
    # ElevenLabs is gone since July 2026 — adding them inflated the report.
    rep.cost = {
        "claude": round(claude, 2),
        "vapi": round(minutes * p.vapi_per_min, 2),
    }
    rep.cost["total"] = round(sum(rep.cost.values()), 2)
    rep.robotaua_block = _robotaua_report_block()
    rep.workua_postings_block = _workua_postings_block()
    rep.balances_block = await _balances_block()

    rep.tg_sent_today, rep.tg_limit, rep.tg_active = await _tg_stats()
    return rep


def _workua_postings_block() -> str:
    """Whether work.ua still carries the postings we recruit for.

    Reads the liveness poller's snapshot — no request of its own. Prints nothing
    while every posting is up, so the day it appears it means something.
    """
    try:
        from src.integrations.workua_liveness import report_block
        return report_block()
    except Exception:
        return ""


def _robotaua_report_block() -> str:
    """robota.ua queue/quota/chat numbers, from the pollers' own snapshot.

    Same source as the bot's /status — reading it costs nothing, while calling
    robota.ua from the report would spend a request against the limit that gets
    this host Cloudflare-challenged.
    """
    try:
        from src.integrations.robotaua_api import read_status
        s = read_status()
    except Exception:
        return ""
    if not s:
        return ""
    quota = s.get("quota_left")
    lines = [
        "",
        "🔍 *robota.ua*",
        f"├ Черга на відкриття контактів: {s.get('pending', '—')}",
        f"├ Квота відкриттів: {quota if quota is not None else '—'}"
        f" (відкрито {s.get('contacts_opened_total', 0)},"
        f" сховали номер {s.get('phones_hidden_total', 0)})",
        f"└ Чат: чекають обробки {s.get('chat_todo', 0)}"
        f" (непрочитаних усього {s.get('chat_unread', 0)}),"
        f" відповідей Єви сьогодні {s.get('chat_replies_today', 0)}",
    ]
    blocked = s.get("responses_blocked_until") or s.get("chat_blocked_until")
    if blocked:
        lines.append(f"  ⏳ Cloudflare пауза до {str(blocked)[11:16]} UTC")
    return "\n".join(lines) + "\n"


async def _balances_block() -> str:
    """What is left of the client's top-ups, at the current burn rate."""
    try:
        import json
        from pathlib import Path as _Path
        from src.cost.summary import balance_forecast

        state_path = _Path(os.getenv("STATE_PATH") or "/tmp/ai_recruiter_state.json")
        balances = json.loads(state_path.read_text(encoding="utf-8")).get("balances") or {}
        rows = await balance_forecast(balances)
    except Exception:
        return ""
    if not rows:
        return ""
    out = ["", "🏦 *Залишки* (ваше поповнення мінус наш облік)"]
    for i, r in enumerate(rows):
        tail = f", на ~{r['days_left']} дн" if r.get("days_left") else ""
        branch = "└" if i == len(rows) - 1 else "├"
        out.append(
            f"{branch} {r['service']}: ${r['left']} з ${r['topped_up']}"
            f" (списано ${r['spent']}{tail})"
        )
    return "\n".join(out) + "\n"



def markdown_safe(text: str) -> str:
    """Neutralise unpaired `_` and `*` so legacy Markdown cannot 400 the digest.

    Telegram's legacy Markdown treats both as toggles. One stray `_` — the word
    `job_id` was enough on 2026-08-22 — opens an italic that never closes, the
    API answers 400 and the whole report is lost.

    Deliberate emphasis is left alone: the digest is built from `*bold*` pairs
    and escaping those would show the asterisks to the reader. Only an odd
    count, which cannot be intentional formatting, gets escaped.
    """
    out = text
    for marker in ("_", "*"):
        if out.count(marker) % 2:
            out = out.replace(marker, "\\" + marker)
    return out

def format_report_md(rep: DayReport) -> str:
    def pct(x: int) -> str:
        return f"{(x / rep.people_dialed * 100):.0f}%" if rep.people_dialed else "0%"

    intake_lines = "\n".join(
        f"├ {label}: {n}" for label, n in sorted(rep.intake_by_source.items())
    ) or "├ немає нових"
    if intake_lines and not intake_lines.startswith("├ немає"):
        # turn the last ├ into └
        head, _, last = intake_lines.rpartition("├")
        intake_lines = head + "└" + last

    total_intake = sum(rep.intake_by_source.values())
    c = rep.cost
    total = c.get("total", 0.0)
    per_qualified = (total / rep.qualified) if rep.qualified else 0.0

    qualified_block = ""
    if rep.qualified_names:
        qualified_block = "\n⭐ *Кваліфіковані:*\n" + "\n".join(
            f"• {n}" for n in rep.qualified_names) + "\n"

    tg_state = "активна" if rep.tg_active else "на паузі"

    return (
        f"📊 *Звіт за {rep.target_date.strftime('%d.%m.%Y')}*\n"
        f"\n"
        f"📞 *Обдзвін* (людей, не спроб)\n"
        f"├ Набирали: {rep.people_dialed} осіб ({rep.attempts} спроб)\n"
        f"├ 💬 Поговорили: {rep.people_talked} ({pct(rep.people_talked)})\n"
        f"├ ⭐ Кваліфіковано: {rep.qualified}\n"
        f"├ 🚫 Не підійшли: {rep.rejected}\n"
        f"├ ⚠️ Кинули на початку: {rep.dropped_early}\n"
        f"├ ❌ Не додзвонились: {rep.unreachable}\n"
        f"└ 🔌 Не з'єдналось: {rep.not_connected}\n"
        f"\n"
        f"⏱ *Час*\n"
        f"├ Сер. розмова: {_fmt_duration(rep.avg_talk_sec)}\n"
        f"└ Всього в лінії: {_fmt_duration(rep.total_in_line_sec)}\n"
        f"\n"
        f"📱 *Telegram-переписка*\n"
        f"├ Написали сьогодні: {rep.tg_sent_today} з {rep.tg_limit}\n"
        f"└ Стан: {tg_state}\n"
        f"\n"
        f"📥 *Нові кандидати: {total_intake}*\n"
        f"{intake_lines}\n"
        f"{rep.robotaua_block}"
        f"{rep.workua_postings_block}"
        f"\n"
        f"💰 *Витрати*\n"
        f"├ Vapi (дзвінки, {rep.total_in_line_sec // 60} хв): ${c.get('vapi', 0):.2f}\n"
        f"├ Anthropic (токени дзвінків): ${c.get('claude', 0):.2f}\n"
        f"└ *Разом: ${total:.2f}*  (${per_qualified:.2f} за кваліфікованого)\n"
        f"{rep.balances_block}"
        f"\n"
        f"🎯 *Воронка зараз*\n"
        f"├ Чекають дзвінка: {rep.funnel_to_call}\n"
        f"├ В роботі: {rep.funnel_calling}\n"
        f"├ ⭐ У рекрутера: {rep.funnel_manager}\n"
        f"├ Не підійшли: {rep.funnel_rejected}\n"
        f"└ Недозвон: {rep.funnel_unreachable}\n"
        + (f"\n🔁 *Перенесено на завтра: {rep.carried_over}*\n"
           "└ не додзвонились або розмова не вийшла — наберемо ще раз\n"
           if rep.carried_over else "")
        +
        f"{qualified_block}"
    )


async def yesterdays_report() -> str:
    return format_report_md(await collect_for(date.today() - timedelta(days=1)))


async def todays_report() -> str:
    return format_report_md(await collect_for(date.today()))
