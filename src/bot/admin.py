"""Admin commands for the Telegram bot.

Authorized users only (chat_id in TG_ADMIN_CHAT_IDS). Lets you:
- /status — services + key counters
- /pause / /resume — gate the call scheduler
- /pause_workua / /resume_workua — gate the work.ua poller
- /queue — funnel snapshot
- /test_call <phone_e164> — trigger one outbound Vapi call
- /params — show key tunables from .env
- /set_threshold <0.0-1.0> — change match_score_threshold in-memory
- /report — send daily report on demand
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

from src.bot.report import format_report_md, collect_for as _collect_for_date
from src.common.db import session_scope
from src.common.models import Candidate, CandidateStatus, Call

log = logging.getLogger("recruiter.bot.admin")

# In-memory control flags. Persisted across restarts via STATE_PATH JSON file.
STATE_PATH = Path("/tmp/ai_recruiter_state.json")

_state: dict[str, Any] = {
    "calls_paused": False,
    "workua_paused": False,
    "match_score_threshold": None,  # None = use settings default
}


def _load_state() -> None:
    if STATE_PATH.exists():
        try:
            import json
            _state.update(json.loads(STATE_PATH.read_text()))
        except Exception:
            pass


def _save_state() -> None:
    try:
        import json
        STATE_PATH.write_text(json.dumps(_state, indent=2))
    except Exception:
        log.exception("save_state_failed")


_load_state()


def calls_paused() -> bool:
    return bool(_state.get("calls_paused"))


def workua_paused() -> bool:
    return bool(_state.get("workua_paused"))


def match_score_override() -> float | None:
    v = _state.get("match_score_threshold")
    return float(v) if v is not None else None


def _is_admin(update: Update) -> bool:
    admins = os.getenv("TG_ADMIN_CHAT_IDS", "").split(",")
    admin_ids = {int(x.strip()) for x in admins if x.strip().lstrip("-").isdigit()}
    # Default: the report chat itself is admin
    chat_id = update.effective_chat.id if update.effective_chat else None
    report_chat = os.getenv("TG_REPORT_CHAT_ID", "")
    if report_chat and report_chat.lstrip("-").isdigit():
        admin_ids.add(int(report_chat))
    return chat_id in admin_ids


async def _guarded(update: Update, ctx: ContextTypes.DEFAULT_TYPE, fn) -> None:
    if not _is_admin(update):
        await update.message.reply_text("⛔ Доступ заборонено.")
        return
    try:
        await fn(update, ctx)
    except Exception as e:
        log.exception("admin_cmd_failed")
        await update.message.reply_text(f"❌ Помилка: {type(e).__name__}: {e}")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        async with session_scope() as session:
            q = await session.execute(
                select(Candidate.status, func.count(Candidate.id)).group_by(Candidate.status)
            )
            funnel = {str(s): int(n) for s, n in q.all()}
            call_count = (await session.execute(select(func.count(Call.id)))).scalar()
        calls_state = "⏸ ПАУЗА" if calls_paused() else "🟢 АКТИВНО"
        wua_state = "⏸ ПАУЗА" if workua_paused() else "🟢 АКТИВНО"
        threshold = match_score_override() or os.getenv("MATCH_SCORE_THRESHOLD", "0.65")
        funnel_lines = "\n".join(f"├ {k}: {v}" for k, v in sorted(funnel.items())) or "├ _(порожньо)_"
        text = (
            "*Статус AI Recruiter*\n\n"
            f"📞 Дзвонилка: {calls_state}\n"
            f"🔍 work.ua пуллер: {wua_state}\n"
            f"🎯 Match threshold: `{threshold}`\n\n"
            f"*Воронка зараз:*\n{funnel_lines}\n\n"
            f"📊 Всього дзвінків в БД: {call_count}\n"
        )
        await u.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await _guarded(update, ctx, _do)


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        _state["calls_paused"] = True
        _save_state()
        await u.message.reply_text("⏸ Дзвонилку зупинено. `/resume` щоб запустити.")
    await _guarded(update, ctx, _do)


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        _state["calls_paused"] = False
        _save_state()
        await u.message.reply_text("🟢 Дзвонилка активна.")
    await _guarded(update, ctx, _do)


async def cmd_pause_workua(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        _state["workua_paused"] = True
        _save_state()
        await u.message.reply_text("⏸ work.ua пуллер зупинено.")
    await _guarded(update, ctx, _do)


async def cmd_resume_workua(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        _state["workua_paused"] = False
        _save_state()
        await u.message.reply_text("🟢 work.ua пуллер активний.")
    await _guarded(update, ctx, _do)


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        async with session_scope() as session:
            q = await session.execute(
                select(Candidate.status, func.count(Candidate.id)).group_by(Candidate.status)
            )
            rows = q.all()
        if not rows:
            await u.message.reply_text("🎯 Воронка порожня.")
            return
        lines = [f"• {s}: {n}" for s, n in rows]
        await u.message.reply_text("🎯 *Черга:*\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    await _guarded(update, ctx, _do)


async def cmd_test_call(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        args = c.args or []
        if not args:
            await u.message.reply_text(
                "Використання: `/test_call +380XXXXXXXXX`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        phone = args[0].strip()
        from src.call.vapi_client import VapiClient
        s_env = os.environ
        vapi = VapiClient()
        try:
            res = await vapi.create_outbound_call(
                assistant_id=s_env["VAPI_ASSISTANT_ID"],
                phone_number_id=s_env["VAPI_PHONE_NUMBER_ID"],
                customer_number_e164=phone,
                metadata={"source": "tg_test_call", "triggered_by": str(u.effective_user.id)},
            )
            call_id = res.get("id", "?")
            await u.message.reply_text(
                f"📞 Тестовий дзвінок ініційовано\n"
                f"call_id: `{call_id}`\n"
                f"номер: `{phone}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        finally:
            await vapi.aclose()
    await _guarded(update, ctx, _do)


async def cmd_params(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        keys = [
            "AGENT_NAME", "COMPANY_NAME", "DEFAULT_VACANCY_TITLE",
            "MATCH_SCORE_THRESHOLD", "CALL_SLOTS", "CALL_MAX_ATTEMPTS",
            "CALL_MAX_CONCURRENT", "CALL_MAX_DURATION_SEC",
            "PROFILE_RECENT_ROLE_YEARS", "PROFILE_WAR_PAUSE_YEAR",
            "REGION_WHITELIST", "REGION_BLACKLIST",
        ]
        lines = []
        for k in keys:
            v = os.getenv(k, "")
            if len(v) > 80:
                v = v[:77] + "..."
            lines.append(f"`{k}` = `{v}`")
        override = match_score_override()
        if override is not None:
            lines.append(f"\n🔧 _Live override:_ match_score = `{override}`")
        await u.message.reply_text(
            "*Поточні параметри:*\n" + "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )
    await _guarded(update, ctx, _do)


async def cmd_set_threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        args = c.args or []
        if not args:
            await u.message.reply_text(
                "Використання: `/set_threshold 0.65` (0.0 - 1.0)",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        try:
            val = float(args[0])
        except ValueError:
            await u.message.reply_text("❌ Має бути число")
            return
        if not 0.0 <= val <= 1.0:
            await u.message.reply_text("❌ Поза діапазоном 0.0-1.0")
            return
        _state["match_score_threshold"] = val
        _save_state()
        await u.message.reply_text(f"🎯 Новий поріг: `{val}`", parse_mode=ParseMode.MARKDOWN)
    await _guarded(update, ctx, _do)


async def cmd_report_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        from datetime import date
        rep = await _collect_for_date(date.today())
        text = format_report_md(rep)
        await u.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await _guarded(update, ctx, _do)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*AI Recruiter — команди*\n\n"
        "📊 Інфо:\n"
        "`/status` — стан всіх компонентів\n"
        "`/queue` — поточна черга\n"
        "`/params` — налаштування .env\n"
        "`/report` — згенерувати звіт зараз\n\n"
        "⏸ Контроль:\n"
        "`/pause` — зупинити дзвонилку\n"
        "`/resume` — продовжити\n"
        "`/pause_workua` — зупинити пуллер work.ua\n"
        "`/resume_workua` — продовжити\n\n"
        "🎯 Налаштування:\n"
        "`/set_threshold 0.65` — поріг match-score\n\n"
        "📞 Тест:\n"
        "`/test_call +380XXXXXXXXX` — тестовий дзвінок\n",
        parse_mode=ParseMode.MARKDOWN,
    )


def register_admin_handlers(app) -> None:
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("pause_workua", cmd_pause_workua))
    app.add_handler(CommandHandler("resume_workua", cmd_resume_workua))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("test_call", cmd_test_call))
    app.add_handler(CommandHandler("params", cmd_params))
    app.add_handler(CommandHandler("set_threshold", cmd_set_threshold))
    app.add_handler(CommandHandler("report", cmd_report_now))
