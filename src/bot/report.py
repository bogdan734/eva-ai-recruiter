"""Daily report aggregator + markdown formatter.

Rewritten 2026-07-21: counts PEOPLE rather than attempts, shows every call
outcome (120 "failed" calls used to vanish from the report entirely), computes
real spend from tokens and minutes, breaks intake down by source, and includes
Telegram outreach.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time as dtime, timedelta

from sqlalchemy import func, select

from src.common import sources as _sources
from src.common.db import session_scope
from src.common.models import Call, Candidate, CandidateStatus
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


# candidates.source is a CSV of every channel a person ever arrived through, so
# one row can legitimately match several of these. The report says so out loud
# rather than pretending the numbers add up to the day's total.
_INTAKE_KINDS: tuple[tuple[str, str], ...] = (
    ("workua_response_send", "work.ua · надіслав резюме"),
    ("workua_response_phonecall", "work.ua · телефонував"),
    ("workua_search", "work.ua · знайшла Єва"),
    ("robotaua_response", "robota.ua · відгук"),
    ("robotaua_chat", "robota.ua · чат"),
    ("googleform", "анкета з форми"),
    ("telegram", "Telegram"),
)


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
    # Finer than intake_by_source, which collapses everything work.ua into one
    # label. This is the line that answers "чому на сайті десять, а в нас
    # п'ятнадцять" without anybody re-counting the cabinet by hand.
    intake_by_kind: dict[str, int] = field(default_factory=dict)
    tg_sent_today: int = 0
    tg_limit: int = 0
    tg_active: bool = False
    funnel_to_call: int = 0
    funnel_calling: int = 0
    funnel_manager: int = 0
    funnel_rejected: int = 0
    funnel_unreachable: int = 0
    # Candidates rolled over to tomorrow's slots. Assigned in collect_for() and
    # read by format_report_md(); it was never declared here, so any other way
    # of building a report crashed the formatter.
    carried_over: int = 0
    qualified_names: list[str] = field(default_factory=list)
    robotaua_block: str = ""
    workua_session_block: str = ""
    workua_postings_block: str = ""
    balances_block: str = ""
    backup_block: str = ""


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


def _kyiv_day_bounds(target: date) -> tuple[datetime, datetime]:
    """[start, end) of a Kyiv calendar day, as UTC-aware timestamps.

    The report is read in Kyiv; the database stores UTC. Comparing on
    `func.date()` silently used the UTC day and moved everything before 03:00
    Kyiv into yesterday.
    """
    from zoneinfo import ZoneInfo

    from src.common.settings import get_settings

    tz = ZoneInfo(get_settings().app_timezone)
    start_local = datetime.combine(target, dtime.min, tzinfo=tz)
    return start_local, start_local + timedelta(days=1)


async def collect_for(target: date) -> DayReport:
    rep = DayReport(target_date=target)
    _day_start, _day_end = _kyiv_day_bounds(target)

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
            .where(Call.started_at >= _day_start, Call.started_at < _day_end)
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
            .where(Candidate.created_at >= _day_start, Candidate.created_at < _day_end)
            .group_by(Candidate.source)
        )).all()
        for raw, n in intake:
            label = source_label(raw)
            rep.intake_by_source[label] = rep.intake_by_source.get(label, 0) + n
            for marker, kind in _INTAKE_KINDS:
                if marker in (raw or ""):
                    rep.intake_by_kind[kind] = rep.intake_by_kind.get(kind, 0) + n

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
                Call.started_at >= _day_start,
                Call.started_at < _day_end,
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
    rep.workua_session_block = _workua_session_block()
    rep.workua_postings_block = await _workua_postings_block()
    rep.backup_block = _backup_block()
    rep.balances_block = await _balances_block()

    rep.tg_sent_today, rep.tg_limit, rep.tg_active = await _tg_stats()
    return rep


async def _workua_postings_block() -> str:
    """Whether work.ua still carries the postings we recruit for.

    Reads the liveness poller's snapshot — no request of its own. Prints nothing
    while every posting is up, so the day it appears it means something.

    2026-09-05: a "starved" verdict here can be stale (see workua_liveness's
    merge_states -- an UNKNOWN probe, e.g. a Cloudflare 403, can never correct
    an old REMOVED). Before repeating a stale alarm, check for real intake in
    the last 3 days for that same vacancy: an actual candidate response is
    ground truth no probe can override.
    """
    try:
        from src.integrations.workua_liveness import report_block, _load_state
        state = _load_state()
        recent_keys = await _recent_workua_intake_keys(days=3)
        text = report_block(state, suppress_keys=recent_keys)
        return text
    except Exception:
        return ""


async def _recent_workua_intake_keys(days: int = 3) -> set[str]:
    """vacancy_key values that received a genuine work.ua response recently.

    Used to catch a liveness false-positive before it repeats in the report:
    a vacancy cannot be "жодного оголошення" if it is still receiving people.
    """
    from datetime import datetime, timedelta

    cutoff = datetime.utcnow() - timedelta(days=days)
    async with session_scope() as session:
        rows = (await session.execute(
            select(Candidate.vacancy_key)
            .where(
                Candidate.created_at >= cutoff,
                Candidate.source.like("workua%"),
            )
            .distinct()
        )).all()
    return {r[0] for r in rows if r[0]}


def _first_future_stamp(*stamps: object) -> object | None:
    """The first of these ISO timestamps that has not passed yet, if any.

    A spent pause left in the report reads as "robota.ua is blocked right now"
    -- the report said "пауза до 14:37 UTC" at 15:30 UTC on 09.09.2026 and cost
    someone a round of investigating a block that had already cleared.
    """
    from datetime import datetime

    now = datetime.utcnow()
    for raw in stamps:
        if not raw:
            continue
        try:
            if datetime.fromisoformat(str(raw)) > now:
                return raw
        except ValueError:
            continue
    return None


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
        f"├ Квота відкриттів на сьогодні: {quota if quota is not None else '—'}"
        f" (відкрито всього за весь час: {s.get('contacts_opened_total', 0)},"
        f" сховали номер: {s.get('phones_hidden_total', 0)})",
        f"└ Чат: чекають обробки {s.get('chat_todo', 0)}"
        f" (непрочитаних усього {s.get('chat_unread', 0)}),"
        f" відповідей Єви сьогодні {s.get('chat_replies_today', 0)}",
    ]
    blocked = _first_future_stamp(
        s.get("responses_blocked_until"), s.get("chat_blocked_until")
    )
    if blocked:
        lines.append(f"  ⏳ Cloudflare пауза до {str(blocked)[11:16]} UTC")
    return "\n".join(lines) + "\n"


def _cold_sourcing_last_run_line() -> str:
    """What the last cold-sourcing run actually ran into.

    Zero candidates because work.ua served its bot-check and zero candidates
    because nobody matched look identical in every other number on this report;
    this is the line that tells them apart.
    """
    import json
    from datetime import datetime
    from pathlib import Path

    from src.common.settings import get_settings

    try:
        path = Path(get_settings().workua_session_state_path).parent / "workua_cold_sourcing.json"
        if not path.exists():
            return "└ ще не запускався\n"
        d = json.loads(path.read_text(encoding="utf-8"))
        when = datetime.utcfromtimestamp(float(d.get("at") or 0)).strftime("%d.%m %H:%M")
        outcome = d.get("outcome")
        if outcome == "challenged":
            return f"└ ⛔️ {when} UTC: work.ua показав бот-перевірку — потрібен проксі\n"
        if outcome == "no_session":
            return f"└ ⚠️ {when} UTC: не було сесії, пошук пропущено\n"
        if outcome == "logged_out":
            return f"└ ⚠️ {when} UTC: браузерна сесія розлогінилась\n"
        return (
            f"└ {when} UTC: переглянуто {d.get('urls_found', 0)} резюме по наших містах, "
            f"пройшли відбір {d.get('after_region_filter', 0)}, "
            f"відкрито контактів {d.get('with_phone', 0)}\n"
        )
    except Exception:
        return ""


def _workua_session_block() -> str:
    """Whether cold sourcing still has a usable work.ua login.

    Reads the saved session file only -- no request of its own. work.ua's `wsid`
    is a sliding ~30-minute window kept alive by the keepalive job; when that
    window closes (host challenged, container down, nobody exported yet) cold
    sourcing silently no-ops, and a silent no-op is indistinguishable in this
    report from a day when nobody matched. So it says so out loud instead.
    """
    import json
    import time
    from pathlib import Path

    from src.common.settings import get_settings

    try:
        s = get_settings()
        path = Path(s.workua_session_state_path)
        if not path.exists():
            return (
                "\n🔎 *Холодний пошук work.ua*\n"
                "└ ❌ немає збереженої сесії — Єва не може шукати резюме\n"
            )
        # Cold sourcing goes through work.ua's API (employer login over HTTPS
        # Basic), not this browser session -- so a dead session is a lost
        # fallback, not a stopped search, and the report must not imply
        # otherwise. On 18.09 a run opened 10 contacts and ingested 5 candidates
        # with this very session signed out.
        via_api = getattr(s, "workua_cold_sourcing_use_api", False)
        head = "\n🔎 *Холодний пошук work.ua*\n"

        # The keepalive's own verdict beats anything inferable from the cookie
        # file: it refreshes `wsid`'s expiry on every beat, so a fresh timestamp
        # only proves a beat happened, not that work.ua still knows us.
        health_path = path.parent / "workua_session_health.json"
        verdict = None
        if health_path.exists():
            try:
                verdict = (json.loads(health_path.read_text(encoding="utf-8")) or {}).get("outcome")
            except Exception:  # noqa: BLE001 — fall back to the cookie check
                verdict = None

        if verdict == "logged_out":
            line = "└ ⚠️ браузерна сесія розлогінилась — потрібен новий експорт cookies\n"
            return head + (
                ("├ ✅ пошук працює через API work.ua\n" + line.replace("└", "├") + _cold_sourcing_last_run_line())
                if via_api else line
            )

        data = json.loads(path.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        wsid = next((c for c in cookies if c.get("name") == "wsid"), None)
        if not wsid:
            return head + "└ ⚠️ у сесії немає ключового cookie (wsid) — потрібен новий експорт\n"
        left_min = (float(wsid.get("expires") or 0) - time.time()) / 60
        if left_min <= 0:
            return head + "└ ❌ сесія протухла — потрібен новий експорт cookies (Cookie-Editor)\n"
        alive = f"├ ✅ сесія жива (ще ~{int(left_min)} хв, продовжується автоматично)\n"
        if via_api:
            alive = "├ ✅ пошук працює через API work.ua\n" + alive
        return head + alive + _cold_sourcing_last_run_line()
    except Exception:
        return ""


def _backup_block() -> str:
    """Whether last night's dump actually happened.

    A backup nobody checks is worse than none — it buys confidence it has not
    earned. So this line is deliberately loud when the dump is missing or stale,
    and quiet when it is fine. Silence here would repeat the mistake the whole
    project keeps making: a counter at zero that nobody reads as a symptom.
    """
    import json
    from datetime import datetime, timedelta

    from src.common.state import state_dir

    path = state_dir() / "backup_status.json"
    if not path.exists():
        return "\n💾 *Бекап бази*\n└ 🔴 не робився жодного разу\n"

    try:
        st = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(str(st.get("at", "")).replace("Z", "+00:00"))
    except Exception:
        return "\n💾 *Бекап бази*\n└ 🔴 статус не читається\n"

    age = datetime.now(UTC) - at
    mb = (st.get("bytes") or 0) / 1_000_000
    when = at.strftime("%d.%m %H:%M")

    if not st.get("ok"):
        why = str(st.get("error") or "")[:80]
        return f"\n💾 *Бекап бази*\n└ 🔴 впав {when} UTC — {why}\n"
    # Cron runs at 03:30; anything older than a day and a half means it stopped.
    if age > timedelta(hours=36):
        return (
            f"\n💾 *Бекап бази*\n"
            f"└ 🔴 останній {when} UTC — {int(age.total_seconds() // 3600)} год тому, крон стоїть\n"
        )
    return f"\n💾 *Бекап бази*\n└ {when} UTC, {mb:.1f} МБ\n"


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

    kind_lines = ""
    if rep.intake_by_kind:
        rows = "\n".join(
            f"│    {k}: {v}" for k, v in sorted(rep.intake_by_kind.items(), key=lambda x: -x[1])
        )
        kind_lines = f"\n│  за типом взаємодії:\n{rows}"

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
        f"├ ⚠️ Кинули на початку: {rep.dropped_early}\n"
        f"└ 🔌 Не з'єдналось: {rep.not_connected}\n"
        f"\n"
        f"📌 *Поточний статус тих, кого сьогодні набирали*\n"
        f"│ (не завжди результат сьогоднішньої розмови — статус міг змінитися\n"
        f"│  й з іншої причини, напр. людина сама залишила новий відгук)\n"
        f"├ ⭐ Кваліфіковано: {rep.qualified}\n"
        f"├ 🚫 Не підійшли: {rep.rejected}\n"
        f"└ ❌ Не додзвонились: {rep.unreachable}\n"
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
        f"{kind_lines}\n"
        f"{rep.robotaua_block}"
        f"{rep.workua_session_block}"
        f"{rep.workua_postings_block}"
        f"\n"
        f"💰 *Витрати*\n"
        f"├ Vapi (дзвінки, {rep.total_in_line_sec // 60} хв): ${c.get('vapi', 0):.2f}\n"
        f"├ Anthropic (токени дзвінків): ${c.get('claude', 0):.2f}\n"
        f"└ *Разом: ${total:.2f}*  (${per_qualified:.2f} за кваліфікованого)\n"
        f"{rep.backup_block}"
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
