"""Telegram daily-report bot — SEPARATE bot, only used to deliver the digest.

Sends a markdown report at 09:00 Europe/Kyiv for the previous day. Provides a small
set of read-only commands so anyone in the report channel can poke the day's state.
"""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from src.bot.admin import register_admin_handlers
from src.bot.report import format_report_md, yesterdays_report
from src.bot.report import collect_for as _collect_for_date
from src.common.settings import get_settings

log = logging.getLogger("recruiter.bot")
logging.basicConfig(level=logging.INFO)


async def _cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "AI Recruiter — керування + звіти.\n\n"
        "`/help` — повний список команд\n"
        "`/status` — стан системи\n"
        "`/queue` — поточна черга\n"
        "`/yesterday` — учорашній звіт\n",
    )


async def _cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from datetime import date

    rep = await _collect_for_date(date.today())
    text = format_report_md(rep)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def _cmd_yesterday(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = await yesterdays_report()
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def _cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from datetime import date

    rep = await _collect_for_date(date.today())
    await update.message.reply_text(
        f"To call: {rep.funnel_to_call}\n"
        f"Calling: {rep.funnel_calling}\n"
        f"Manager review: {rep.funnel_manager}",
    )


async def _send_daily(app: Application) -> None:
    s = get_settings()
    if not s.tg_report_chat_id:
        log.warning("daily.skip", reason="no_chat_id_configured")
        return
    text = await yesterdays_report()
    await app.bot.send_message(
        chat_id=s.tg_report_chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )
    log.info("daily.sent", chat=s.tg_report_chat_id)


def build_app() -> Application:
    s = get_settings()
    if not s.tg_report_bot_token:
        raise RuntimeError("TG_REPORT_BOT_TOKEN not configured")
    app = Application.builder().token(s.tg_report_bot_token).build()
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("today", _cmd_today))
    app.add_handler(CommandHandler("yesterday", _cmd_yesterday))
    register_admin_handlers(app)
    return app


async def _run() -> None:
    s = get_settings()
    app = build_app()
    scheduler = AsyncIOScheduler(timezone=s.app_timezone)
    scheduler.add_job(
        _send_daily,
        trigger=CronTrigger(hour=s.tg_report_hour, minute=s.tg_report_minute),
        kwargs={"app": app},
        id="daily_report",
        replace_existing=True,
    )
    scheduler.start()
    log.info(
        "bot.start schedule=%02d:%02d %s",
        s.tg_report_hour, s.tg_report_minute, s.app_timezone,
    )
    async with app:
        await app.start()
        await app.updater.start_polling()
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(_run())
