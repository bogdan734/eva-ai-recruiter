"""Admin commands for the Telegram bot.

Authorized users only (chat_id in TG_ADMIN_CHAT_IDS). Lets you:
- /status — services + key counters
- /pause / /resume — gate the call scheduler
- /pause_workua / /resume_workua — gate the work.ua poller
- /pause_robotaua / /resume_robotaua — gate the robota.ua poller
- /costs, /set_balance — metered spend and what is left of a top-up
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
from datetime import datetime
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
STATE_PATH = Path(os.getenv("STATE_PATH", "/tmp/ai_recruiter_state.json"))

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
    _load_state()  # re-read shared file so cross-process toggles take effect live
    return bool(_state.get("calls_paused"))


def workua_paused() -> bool:
    _load_state()
    return bool(_state.get("workua_paused"))


def robotaua_paused() -> bool:
    _load_state()
    return bool(_state.get("robotaua_paused"))


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
        rua_state = "⏸ ПАУЗА" if robotaua_paused() else "🟢 АКТИВНО"
        rua_pending = _robotaua_pending_count()
        rua_line = _robotaua_block(rua_state)
        threshold = match_score_override() or os.getenv("MATCH_SCORE_THRESHOLD", "0.65")
        funnel_lines = "\n".join(f"├ {k}: {v}" for k, v in sorted(funnel.items())) or "├ _(порожньо)_"
        text = (
            "*Статус AI Recruiter*\n\n"
            f"📞 Дзвонилка: {calls_state}\n"
            f"🔍 work.ua пуллер: {wua_state}\n"
            f"{rua_line}\n"
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


def _robotaua_pending_count() -> int:
    """Applies whose phone robota.ua keeps behind "open contacts" — they sit in
    the poller's cursor file until someone opens the contact in the cabinet."""
    try:
        from src.integrations.robotaua_sync import load_cursor
        return len(load_cursor().get("pending") or {})
    except Exception:
        return 0


async def cmd_pause_robotaua(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        _state["robotaua_paused"] = True
        _save_state()
        await u.message.reply_text("⏸ robota.ua пуллер зупинено.")
    await _guarded(update, ctx, _do)


async def cmd_resume_robotaua(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        _state["robotaua_paused"] = False
        _save_state()
        await u.message.reply_text("🟢 robota.ua пуллер активний.")
    await _guarded(update, ctx, _do)


def _robotaua_block(state_label: str) -> str:
    """robota.ua numbers for /status, read from the poller's snapshot file.

    Never calls robota.ua: their Cloudflare rate-limits us, and a /status press
    must not spend one of the few requests a poll gets.
    """
    try:
        from src.integrations.robotaua_api import read_status
        s = read_status()
    except Exception:
        s = {}
    if not s:
        return f"🔍 robota.ua: {state_label}"

    def when(key: str) -> str:
        raw = s.get(key)
        if not raw:
            return "—"
        try:
            dt = datetime.fromisoformat(str(raw)[:19])
        except ValueError:
            return "—"
        mins = int((datetime.utcnow() - dt).total_seconds() // 60)
        return f"{mins} хв тому" if mins < 90 else dt.strftime("%d.%m %H:%M")

    lines = [f"🔍 robota.ua: {state_label}"]
    lines.append(
        f"├ відгуки: черга контактів {s.get('pending', '—')}, "
        f"останній збір {when('responses_last_ok')}"
    )
    quota = s.get("quota_left")
    lines.append(
        f"├ квота відкриттів: {quota if quota is not None else '—'}"
        f" (відкрито {s.get('contacts_opened_total', 0)},"
        f" сховали номер {s.get('phones_hidden_total', 0)})"
    )
    lines.append(
        f"├ чат: чекають обробки {s.get('chat_todo', 0)}"
        f" (непрочитаних усього {s.get('chat_unread', 0)}),"
        f" відповіді сьогодні {s.get('chat_replies_today', 0)},"
        f" останній збір {when('chat_last_ok')}"
    )
    blocked = s.get("responses_blocked_until") or s.get("chat_blocked_until")
    if blocked:
        lines.append(f"└ ⏳ Cloudflare пауза до {str(blocked)[11:16]} UTC")
    return "\n".join(lines)


async def cmd_costs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        from datetime import timedelta
        from src.cost.summary import balance_forecast, spend_since

        today = await spend_since(datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0))
        week = await spend_since(datetime.utcnow() - timedelta(days=7))
        lines = [
            "*Гроші*",
            "",
            f"*Сьогодні:* {today.calls} дзвінків, {today.minutes} хв → *${today.total}*",
            "  " + ", ".join(f"{k} ${v}" for k, v in today.per_service.items()),
            f"*7 днів:* {week.calls} дзвінків, {week.minutes} хв → *${week.total}*",
            "  " + ", ".join(f"{k} ${v}" for k, v in week.per_service.items()),
        ]

        forecast = await balance_forecast(_state.get("balances") or {})
        if forecast:
            lines += ["", "*Залишки* (від вашого поповнення мінус наші витрати):"]
            for f in forecast:
                tail = f"на ~{f['days_left']} дн" if f.get("days_left") else "витрат нема"
                lines.append(
                    f"├ {f['service']}: було ${f['topped_up']} ({f['at']}),"
                    f" списано ${f['spent']} → *${f['left']}*, {tail}"
                )
        else:
            lines += [
                "",
                "_Балансів не задано._ Після поповнення надішліть:",
                "`/set_balance vapi 20` або `/set_balance anthropic 25`",
            ]
        # Where the Anthropic tokens actually went this week. Until 04.08 only the
        # post-call summaries were counted, so this line is the honest picture.
        from src.cost.usage import breakdown_since

        parts = await breakdown_since(datetime.utcnow() - timedelta(days=7))
        if parts:
            named = {
                "name_origin": "перевірка імен на інтейку",
                "scorer": "оцінка відповідності",
                "tg_userbot": "переписка в Telegram",
            }
            lines += ["", "*Токени поза дзвінками* (7 днів):"]
            for p in parts:
                total_tok = p["tokens_in"] + p["tokens_out"]
                lines.append(
                    f"├ {named.get(p['component'], p['component'])}: {total_tok:,} ток."
                    .replace(",", " ")
                )
            lines.append(
                f"└ разом з дзвінками: {week.tokens_in + week.off_call_tokens_in:,} вх / "
                f"{week.tokens_out + week.off_call_tokens_out:,} вих".replace(",", " ")
            )

        lines += [
            "",
            "_Оцінка за тарифами `pricing.py`, а не рахунок вендора._",
            "_Anthropic тепер рахує і дзвінки, і роботу Єви між ними "
            "(перевірка імен, оцінка, Telegram). У чаті robota.ua відповіді "
            "шаблонні — токенів там нема._",
            "_Токени самої розмови всередині Vapi нам не віддаються — на цю "
            "частину рахунку ми не бачимо._",
            "_Черга й квота robota.ua — у `/status`._",
        ]
        await u.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    await _guarded(update, ctx, _do)


async def cmd_set_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _do(u, c):
        args = (c.args or [])
        if len(args) < 2:
            await u.message.reply_text(
                "Формат: `/set_balance <vapi|anthropic|deepgram> <сума>`\n"
                "Напр. `/set_balance vapi 20` — після поповнення на 20 доларів.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        service = args[0].strip().lower()
        try:
            usd = float(args[1].replace(",", "."))
        except ValueError:
            await u.message.reply_text("Сума має бути числом, напр. `20` або `17.98`.")
            return
        balances = dict(_state.get("balances") or {})
        balances[service] = {"usd": usd, "at": datetime.utcnow().isoformat(timespec="seconds")}
        _state["balances"] = balances
        _save_state()
        await u.message.reply_text(
            f"✅ {service}: ${usd:.2f} зафіксовано. Далі рахую списання від цього моменту — `/costs`."
        )
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
    """Trigger an outbound test call with a fully configurable candidate.

    Usage:
      /test_call +380XXXXXXXXX
      /test_call +380XXXXXXXXX name=Олег position=менеджер region=Дніпро
      keys: name, position, region, vacancy  (values may contain spaces)
    """
    async def _do(u, c):
        args = c.args or []
        if not args:
            await u.message.reply_text(
                "Використання:\n"
                "<code>/test_call +380XXXXXXXXX</code>\n"
                "<code>/test_call +380XXXXXXXXX name=Олег position=менеджер region=Дніпро</code>\n"
                "Ключі: name, position, region, vacancy",
                parse_mode=ParseMode.HTML,
            )
            return
        phone = args[0].strip()
        if not phone.startswith("+"):
            await u.message.reply_text(
                "Перший аргумент — номер у форматі <code>+380XXXXXXXXX</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        KEYS = {"name", "position", "region", "vacancy"}
        fields = {}
        cur = None
        for tok in args[1:]:
            if "=" in tok and tok.split("=", 1)[0] in KEYS:
                k, v = tok.split("=", 1)
                cur = k
                fields[cur] = v
            elif cur:
                fields[cur] += " " + tok
        for k in list(fields):
            fields[k] = fields[k].strip()

        from src.call.script_template import render_system_prompt
        from src.call.vapi_client import VapiClient

        name = fields.get("name") or "невідомий (тестовий дзвінок)"
        position = fields.get("position") or "невідомо (запитати у кандидата)"
        region = fields.get("region") or "невідомо (запитати у кандидата)"
        vacancy = fields.get("vacancy")

        prompt = render_system_prompt(
            candidate_name=name,
            candidate_phone=phone,
            candidate_position=position,
            candidate_region=region,
            source="tg_test_call",
            vacancy_title=vacancy,
        )
        overrides = {
            "model": {
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "messages": [{"role": "system", "content": prompt}],
            },
                "variableValues": {
                "candidate_name": name,
                "candidate_position": position,
                "candidate_region": region,
            },
        }

        s_env = os.environ
        vapi = VapiClient()
        try:
            res = await vapi.create_outbound_call(
                assistant_id=s_env["VAPI_ASSISTANT_ID"],
                phone_number_id=s_env["VAPI_PHONE_NUMBER_ID"],
                customer_number_e164=phone,
                assistant_overrides=overrides,
                metadata={"source": "tg_test_call", "triggered_by": str(u.effective_user.id)},
            )
            call_id = res.get("id", "?")
            await u.message.reply_text(
                "\U0001F4DE Тестовий дзвінок ініційовано\n"
                f"call_id: <code>{call_id}</code>\n"
                f"номер: <code>{phone}</code>\n"
                f"кандидат: <b>{name}</b>\n"
                f"посада: {position}\n"
                f"регіон: {region}"
                + (f"\nвакансія: {vacancy}" if vacancy else ""),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await u.message.reply_text(
                f"\u274C Помилка дзвінка: <code>{type(e).__name__}: {str(e)[:300]}</code>",
                parse_mode=ParseMode.HTML,
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
        "`/resume_workua` — продовжити\n"
        "`/costs` — витрати й залишки на балансах\n"
        "`/set_balance <сервіс> <сума>` — зафіксувати баланс після поповнення\n"
        "`/pause_robotaua` — зупинити пуллер robota.ua\n"
        "`/resume_robotaua` — продовжити\n\n"
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
    app.add_handler(CommandHandler("pause_robotaua", cmd_pause_robotaua))
    app.add_handler(CommandHandler("resume_robotaua", cmd_resume_robotaua))
    app.add_handler(CommandHandler("costs", cmd_costs))
    app.add_handler(CommandHandler("set_balance", cmd_set_balance))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("test_call", cmd_test_call))
    app.add_handler(CommandHandler("params", cmd_params))
    app.add_handler(CommandHandler("set_threshold", cmd_set_threshold))
    app.add_handler(CommandHandler("report", cmd_report_now))
