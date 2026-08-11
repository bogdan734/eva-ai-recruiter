"""Button-driven UI for the recruiter bot — no commands to memorize.

Everything the operator needs (test calls, candidate criteria, vacancy params,
status, pause) is reachable through inline keyboards. Criteria edits are applied
live: they write to the shared _state, mirror into os.environ, and clear the
settings cache so the matcher/scorer pick them up immediately. On startup the
saved overrides are re-applied.
"""
from __future__ import annotations

import os
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.bot.admin import _is_admin, _state, _save_state, calls_paused
from src.common import vacancies as _vacancies
from src.common import vacancy_store as _vstore
from src.common.settings import get_settings

# criteria fields the menu can edit -> the env var the Settings model reads
_CRIT_ENV = {
    "rw": "REGION_WHITELIST",
    "rb": "REGION_BLACKLIST",
    "thr": "MATCH_SCORE_THRESHOLD",
    "agef_min": "PROFILE_AGE_MIN_F",
    "agef_max": "PROFILE_AGE_MAX_F",
    "agem_min": "PROFILE_AGE_MIN_M",
    "agem_max": "PROFILE_AGE_MAX_M",
    "vac_title": "DEFAULT_VACANCY_TITLE",
    "vac_salary": "DEFAULT_VACANCY_SALARY",
    "vac_schedule": "DEFAULT_VACANCY_SCHEDULE",
    "vac_benefits": "DEFAULT_VACANCY_BENEFITS",
}


# Available outbound trunks. They show DIFFERENT numbers to the candidate —
# switching here changes the caller ID, so do not treat them as interchangeable.
# (The old comment here claimed both carried +380673350196; that was wrong.)
_TRUNKS = {
    # StreamTelecom, credential 6f7adf61 — the live one.
    "streamtele": ("5284b036-c03c-4455-a53a-57819d0c3c3b", "StreamTelecom +380935824369 (бойовий)"),
    # Ringostat FMC, credential 376be354 — different CLI.
    "ringostat": ("d4c72be0-8db7-41f3-8586-acf28a5afb4e", "Ringostat +380673350196 (резерв)"),
}


def _current_trunk_key() -> str:
    pid = os.environ.get("VAPI_PHONE_NUMBER_ID", "")
    for k, (tid, _name) in _TRUNKS.items():
        if tid == pid:
            return k
    return _state.get("trunk", "streamtele")


def _set_trunk(key: str) -> None:
    if key not in _TRUNKS:
        return
    tid, _name = _TRUNKS[key]
    _state["trunk"] = key
    _save_state()
    os.environ["VAPI_PHONE_NUMBER_ID"] = tid


def _apply_override(key: str, value: str) -> None:
    """Persist a criteria override and make it live immediately."""
    env = _CRIT_ENV[key]
    _state.setdefault("criteria", {})[key] = value
    _save_state()
    os.environ[env] = value
    get_settings.cache_clear()


def apply_saved_overrides() -> None:
    """Re-apply saved criteria into env at startup (before first get_settings)."""
    for key, value in (_state.get("criteria") or {}).items():
        if key in _CRIT_ENV:
            os.environ[_CRIT_ENV[key]] = value
    get_settings.cache_clear()


# ------------------------------- keyboards --------------------------------

def _main_kb() -> InlineKeyboardMarkup:
    calls_lbl = "▶️ Увімкнути дзвінки" if calls_paused() else "⏸ Пауза дзвінків"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📞 Тестовий дзвінок", callback_data="call:new")],
        [InlineKeyboardButton("🎯 Критерії кандидатів", callback_data="nav:crit")],
        [InlineKeyboardButton("💼 Параметри вакансії", callback_data="nav:vac")],
        [InlineKeyboardButton("🔎 Пошук резюме", callback_data="srch:pick")],
        [InlineKeyboardButton("📊 Статус", callback_data="act:status"),
         InlineKeyboardButton("📋 Звіт", callback_data="act:report")],
        [InlineKeyboardButton(calls_lbl, callback_data="act:toggle_calls")],
        [InlineKeyboardButton(f"\U0001F500 \u0422\u0440\u0430\u043d\u043a: {_TRUNKS[_current_trunk_key()][1]}", callback_data="act:trunk")],
        [InlineKeyboardButton("\U0001F4E8 Telegram \u0404\u0432\u0430", callback_data="nav:tg")],
        [InlineKeyboardButton("\U0001F4E5 \u0414\u043E\u0434\u0430\u0442\u0438 \u043A\u0430\u043D\u0434\u0438\u0434\u0430\u0442\u0456\u0432", callback_data="cand:add")],
    ])


def _crit_kb() -> InlineKeyboardMarkup:
    s = get_settings()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎯 Поріг відбору: {s.match_score_threshold}", callback_data="nav:thr")],
        [InlineKeyboardButton("✅ Дозволені регіони", callback_data="ed:rw")],
        [InlineKeyboardButton("⛔ Заборонені регіони", callback_data="ed:rb")],
        [InlineKeyboardButton(f"👩 Вік (жінки): {s.profile_age_min_f}–{s.profile_age_max_f}", callback_data="ed:agef")],
        [InlineKeyboardButton(f"👨 Вік (чоловіки): {s.profile_age_min_m}–{s.profile_age_max_m}", callback_data="ed:agem")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="nav:main")],
    ])


def _thr_kb() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(f"{v}", callback_data=f"setthr:{v}")
           for v in ("0.50", "0.60", "0.65", "0.70", "0.75", "0.80")]
    return InlineKeyboardMarkup([row[:3], row[3:], [InlineKeyboardButton("⬅️ Назад", callback_data="nav:crit")]])


def _vac_global_kb() -> InlineKeyboardMarkup:
    """The original global fields — now the fallback under every vacancy."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📌 Назва вакансії", callback_data="ed:vac_title")],
        [InlineKeyboardButton("💰 Зарплата", callback_data="ed:vac_salary")],
        [InlineKeyboardButton("🗓 Графік", callback_data="ed:vac_schedule")],
        [InlineKeyboardButton("🎁 Умови/бонуси", callback_data="ed:vac_benefits")],
        [InlineKeyboardButton("⬅️ До списку вакансій", callback_data="nav:vac")],
    ])


def _vac_list_text() -> str:
    lines = ["💼 <b>Вакансії</b>", ""]
    for key, vac in _vacancies.all_vacancies().items():
        described = _vstore.describe(_vacancies.get(key))
        own = sum(1 for f in _vstore.EDITABLE if described[f]["source"] == "панель")
        state = "Єва дзвонить" if vac.calls_enabled else "лише збір відгуків"
        detail = f"свій текст: {own} з {len(_vstore.EDITABLE)} полів" if own else "текст загальний"
        lines.append(f"• <b>{vac.label}</b> — {state}, {detail}")
    lines += [
        "",
        "Оберіть вакансію, щоб змінити, що Єва про неї говорить.",
        "«Загальні» — текст для тих вакансій, які свого не мають.",
    ]
    return "\n".join(lines)


def _vac_kb() -> InlineKeyboardMarkup:
    """Vacancy picker. Editing per vacancy is the point — before this every
    candidate heard one global text whichever posting they answered."""
    rows = []
    for key, vac in _vacancies.all_vacancies().items():
        calls = "📞" if vac.calls_enabled else "📥"
        rows.append([InlineKeyboardButton(f"{calls} {vac.label}", callback_data=f"vac:{key}")])
    rows.append([InlineKeyboardButton("➕ Нова вакансія", callback_data="vacnew")])
    rows.append([InlineKeyboardButton("🌐 Загальні (для всіх)", callback_data="vac:__global__")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="nav:main")])
    return InlineKeyboardMarkup(rows)


_OPS_ICONS = {
    "label": "🏷",
    "workua_ids": "🔵",
    "robotaua_ids": "🟣",
    "keycrm_pipeline_id": "📂",
    "keycrm_status_id": "📍",
    "calls_enabled": "📞",
    "screen_enabled": "🎯",
    "intake_enabled": "📥",
    "open_paid_contacts": "💳",
    "vacancy_url": "🔗",
}


def _ops_value(vac, field: str) -> str:
    v = getattr(vac, field, None)
    if field in _vstore.BOOL_FIELDS:
        return "так" if v else "ні"
    if field in _vstore.IDSET_FIELDS:
        return ", ".join(str(x) for x in sorted(v or ())) or "—"
    return str(v or "—")


def _vac_ops_text(key: str) -> str:
    vac = _vacancies.get(key)
    lines = [f"⚙️ <b>{vac.label}</b> — збір і обдзвін", ""]
    for f in _vstore.OPS_FIELDS:
        lines.append(f"{_OPS_ICONS[f]} <b>{_vstore.OPS_LABELS[f]}</b>: {_ops_value(vac, f)}")
    problems = _vstore.blockers(vac)
    lines.append("")
    if problems:
        lines.append("⚠️ <b>Не готова до роботи:</b>")
        lines += [f"   • {p}" for p in problems]
        lines.append("")
        lines.append("Поки це не заповнено, вмикати збір і дзвінки не можна.")
    elif vac.intake_enabled:
        lines.append("✅ Збір відгуків працює.")
        lines.append("📞 Єва дзвонить." if vac.calls_enabled else "📥 Дзвінків немає — лише картки в CRM.")
    else:
        lines.append("⏸ Збір вимкнено — відгуки не потрапляють у CRM.")
    return "\n".join(lines)


def _vac_ops_kb(key: str) -> InlineKeyboardMarkup:
    vac = _vacancies.get(key)
    rows = []
    for f in _vstore.OPS_FIELDS:
        rows.append([InlineKeyboardButton(
            f"{_OPS_ICONS[f]} {_vstore.OPS_LABELS[f]}: {_ops_value(vac, f)[:22]}",
            callback_data=f"vacof:{key}:{f}",
        )])
    if _vstore.is_custom(key):
        rows.append([InlineKeyboardButton("🗑 Видалити вакансію", callback_data=f"vacdel:{key}")])
    rows.append([InlineKeyboardButton("⬅️ До вакансії", callback_data=f"vac:{key}")])
    return InlineKeyboardMarkup(rows)


_OPS_HINTS = {
    "label": "Назва, як вона стоїть у KeyCRM у полі «Вакансія».",
    "workua_ids": (
        "ID вакансій на work.ua через кому. Це число з посилання: "
        "<code>work.ua/jobs/<b>8249916</b>/</code>\nПорожньо — не збирати з work.ua."
    ),
    "robotaua_ids": (
        "ID вакансій на robota.ua через кому — число з посилання на вакансію "
        "в кабінеті.\nПорожньо — не збирати з robota.ua."
    ),
    "keycrm_pipeline_id": "Номер воронки в KeyCRM, куди складати картки.",
    "keycrm_status_id": "Номер етапу в цій воронці, на який картка потрапляє першою.",
    "vacancy_url": "Посилання на оголошення — його видно в картці кандидата.",
}


_VAC_ICONS = {
    "spoken_title": "📌",
    "spoken_salary": "💰",
    "spoken_schedule": "🗓",
    "spoken_benefits": "🎁",
    "spoken_pitch": "🏢",
}


def _vac_one_kb(key: str) -> InlineKeyboardMarkup:
    vac = _vacancies.get(key)
    described = _vstore.describe(vac)
    rows = []
    for field in _vstore.EDITABLE:
        label = _vstore.LABELS[field]
        row = [InlineKeyboardButton(
            f"{_VAC_ICONS[field]} {label}", callback_data=f"vacf:{key}:{field}"
        )]
        # Reset only where there is an override to reset. A blank spacer button
        # would keep the columns even, but Telegram rejects whitespace-only text.
        if described[field]["source"] == "панель":
            row.append(InlineKeyboardButton("↩️", callback_data=f"vacr:{key}:{field}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⚙️ Збір і обдзвін", callback_data=f"vacops:{key}")])
    rows.append([InlineKeyboardButton("⬅️ До списку вакансій", callback_data="nav:vac")])
    return InlineKeyboardMarkup(rows)


def _vac_one_text(key: str) -> str:
    vac = _vacancies.get(key)
    s = get_settings()
    globals_ = {
        "spoken_title": s.default_vacancy_title,
        "spoken_salary": s.default_vacancy_salary,
        "spoken_schedule": s.default_vacancy_schedule,
        "spoken_benefits": s.default_vacancy_benefits,
        "spoken_pitch": s.company_pitch,
    }
    lines = [f"💼 <b>{vac.label}</b>", ""]
    if not vac.calls_enabled:
        lines.append("📥 Єва цю вакансію <b>не обдзвонює</b> — лише збирає відгуки.")
        lines.append("")
    for field in _vstore.EDITABLE:
        info = _vstore.describe(vac)[field]
        value = info["value"] or globals_.get(field, "")
        mark = "" if info["source"] == "панель" else "  <i>(загальне)</i>"
        shown = (value or "—")[:110]
        lines.append(f"{_VAC_ICONS[field]} <b>{_vstore.LABELS[field]}</b>{mark}\n{shown}")
    lines.append("")
    lines.append("Натисніть поле, щоб змінити. ↩️ повертає до загального значення.")
    lines.append("⚠️ Єва це <b>вимовляє</b> — числа пишіть словами.")
    return "\n".join(lines)


def _call_kb(cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    def lbl(icon, name, key):
        return f"{icon} {name}: {cfg.get(key) or '—'}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lbl("👤", "Ім'я", "name"), callback_data="callf:name")],
        [InlineKeyboardButton(lbl("💼", "Посада", "position"), callback_data="callf:position")],
        [InlineKeyboardButton(lbl("📍", "Регіон", "region"), callback_data="callf:region")],
        [InlineKeyboardButton("📞 Подзвонити зараз", callback_data="call:go")],
        [InlineKeyboardButton("⬅️ Скасувати", callback_data="call:cancel")],
    ])


# ------------------------------- screens ----------------------------------

def _main_text() -> str:
    return (
        "🤖 <b>AI Recruiter — панель керування</b>\n\n"
        "Оберіть дію кнопкою нижче. Команди знати не треба."
    )


def _crit_text() -> str:
    s = get_settings()
    return (
        "🎯 <b>Критерії кандидатів</b>\n\n"
        f"Поріг відбору (match-score): <b>{s.match_score_threshold}</b>\n"
        f"Дозволені регіони: <i>{s.region_whitelist[:200]}</i>\n"
        f"Заборонені регіони: <i>{s.region_blacklist[:200]}</i>\n"
        f"Вік жінки: <b>{s.profile_age_min_f}–{s.profile_age_max_f}</b>, "
        f"чоловіки: <b>{s.profile_age_min_m}–{s.profile_age_max_m}</b>\n\n"
        "Натисніть параметр, щоб змінити."
    )


def _vac_text() -> str:
    s = get_settings()
    return (
        "💼 <b>Параметри вакансії</b>\n\n"
        f"📌 {s.default_vacancy_title}\n"
        f"💰 {s.default_vacancy_salary}\n"
        f"🗓 {s.default_vacancy_schedule}\n"
        f"🎁 {s.default_vacancy_benefits}\n\n"
        "Натисніть, щоб змінити поле."
    )


_PROMPTS = {
    "rw": "Надішліть список ДОЗВОЛЕНИХ регіонів через кому.\nНапр.: <code>Київська,Львівська,Вінницька</code>",
    "rb": "Надішліть список ЗАБОРОНЕНИХ регіонів через кому.\nНапр.: <code>м. Київ,Донецька,Херсонська</code>",
    "agef": "Надішліть діапазон віку жінок: <code>мін-макс</code>. Напр.: <code>23-42</code>",
    "agem": "Надішліть діапазон віку чоловіків: <code>мін-макс</code>. Напр.: <code>23-40</code>",
    "vac_title": "Надішліть нову назву вакансії.",
    "vac_salary": "Надішліть опис зарплати.",
    "vac_schedule": "Надішліть графік роботи.",
    "vac_benefits": "Надішліть умови/бонуси.",
    "call_name": "Надішліть ім'я кандидата.",
    "call_position": "Надішліть посаду/напрям.",
    "call_region": "Надішліть регіон кандидата.",
    "call_phone": "Надішліть номер кандидата у форматі <code>+380XXXXXXXXX</code>.",
}



TG_CTRL_URL = os.environ.get("TG_CONTROL_URL", "http://tguserbot:8090")


async def _tg_get(path: str) -> dict:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(TG_CTRL_URL + path)
            return r.json()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def _tg_post(path: str, body: dict | None = None) -> dict:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(TG_CTRL_URL + path, json=body or {})
            return r.json()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _tg_kb(active: bool) -> InlineKeyboardMarkup:
    toggle = "\u23F8 \u041F\u043E\u0441\u0442\u0430\u0432\u0438\u0442\u0438 \u043D\u0430 \u043F\u0430\u0443\u0437\u0443" if active else "\u25B6\uFE0F \u0423\u0432\u0456\u043C\u043A\u043D\u0443\u0442\u0438"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2709\uFE0F \u041D\u0430\u043F\u0438\u0441\u0430\u0442\u0438 \u043A\u0430\u043D\u0434\u0438\u0434\u0430\u0442\u0443", callback_data="tg:send")],
        [InlineKeyboardButton(toggle, callback_data="tg:toggle")],
        [InlineKeyboardButton("\u2B05\uFE0F \u041D\u0430\u0437\u0430\u0434", callback_data="nav:main")],
    ])


async def _tg_text() -> str:
    h = await _tg_get("/health")
    if h.get("error"):
        return f"\U0001F4E8 <b>Telegram \u0404\u0432\u0430</b>\n\n\u26A0\uFE0F \u041D\u0435\u043C\u0430\u0454 \u0437\u0432\u2019\u044F\u0437\u043A\u0443 \u0437 \u044E\u0437\u0435\u0440\u0431\u043E\u0442\u043E\u043C:\n<code>{h['error']}</code>"
    st = "\U0001F7E2 \u0430\u043A\u0442\u0438\u0432\u043D\u0430" if h.get("active") else "\u23F8 \u043F\u0430\u0443\u0437\u0430"
    return (
        "\U0001F4E8 <b>Telegram \u0404\u0432\u0430</b> \u2014 \u043F\u0435\u0440\u0435\u043F\u0438\u0441\u043A\u0430 \u044F\u043A \u0436\u0438\u0432\u0430 \u043B\u044E\u0434\u0438\u043D\u0430\n\n"
        f"\u0410\u043A\u0430\u0443\u043D\u0442: <b>{h.get('me','?')}</b>\n"
        f"\u0421\u0442\u0430\u043D: {st}\n"
        f"\u041D\u043E\u0432\u0438\u0445 \u0434\u0456\u0430\u043B\u043E\u0433\u0456\u0432 \u0441\u044C\u043E\u0433\u043E\u0434\u043D\u0456: {h.get('sent_today','?')}/{h.get('limit','?')}"
    )


# ------------------------------- handlers ---------------------------------

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await update.message.reply_text("⛔ Доступ заборонено.")
        return
    ctx.user_data.pop("await", None)
    await update.message.reply_text(_main_text(), reply_markup=_main_kb(), parse_mode=ParseMode.HTML)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _is_admin(update):
        await q.answer("⛔", show_alert=True)
        return
    await q.answer()
    data = q.data or ""

    async def edit(text, kb):
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            await q.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

    # navigation
    if data == "nav:main":
        ctx.user_data.pop("await", None)
        return await edit(_main_text(), _main_kb())
    if data == "nav:crit":
        return await edit(_crit_text(), _crit_kb())
    if data == "nav:vac":
        return await edit(_vac_list_text(), _vac_kb())
    if data == "noop":
        return

    # ---- per-vacancy script ----
    if data.startswith("vac:"):
        key = data.split(":", 1)[1]
        if key == "__global__":
            # The old global fields, still the fallback for any vacancy that has
            # not set its own text.
            return await edit(_vac_text(), _vac_global_kb())
        return await edit(_vac_one_text(key), _vac_one_kb(key))

    if data.startswith("vacf:"):
        _, key, field = data.split(":", 2)
        ctx.user_data["await"] = f"vacf:{key}:{field}"
        vac = _vacancies.get(key)
        return await edit(
            f"✏️ <b>{_vstore.LABELS[field]}</b> — {vac.label}\n\n"
            "Надішліть новий текст.\n\n"
            "⚠️ Єва це вимовляє вголос: числа словами "
            "(<code>двадцять п’ять тисяч</code>, не <code>25000</code>), "
            "латиницю уникайте (<code>бі-ту-бі</code>, не <code>B2B</code>).",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Скасувати", callback_data=f"vac:{key}")]]),
        )

    if data.startswith("vacr:"):
        _, key, field = data.split(":", 2)
        _vstore.clear_field(key, field)
        return await edit(_vac_one_text(key), _vac_one_kb(key))

    # ---- how the vacancy is worked: boards, funnel, calls ----
    if data.startswith("vacops:"):
        key = data.split(":", 1)[1]
        return await edit(_vac_ops_text(key), _vac_ops_kb(key))

    if data.startswith("vacof:"):
        _, key, field = data.split(":", 2)
        vac = _vacancies.get(key)
        # Booleans are a tap, not a typing exercise — and a typo in "так" would
        # otherwise read as "ні" and quietly switch off someone's intake.
        if field in _vstore.BOOL_FIELDS:
            if field in ("intake_enabled", "calls_enabled") and not getattr(vac, field):
                problems = _vstore.blockers(vac)
                if problems:
                    return await edit(
                        "⚠️ Спершу заповніть:\n" + "\n".join(f"• {p}" for p in problems),
                        _vac_ops_kb(key),
                    )
            _vstore.set_field(key, field, "ні" if getattr(vac, field) else "так")
            return await edit(_vac_ops_text(key), _vac_ops_kb(key))
        ctx.user_data["await"] = f"vacof:{key}:{field}"
        return await edit(
            f"✏️ <b>{_vstore.OPS_LABELS[field]}</b> — {vac.label}\n\n"
            f"Зараз: <code>{_ops_value(vac, field)}</code>\n\n" + _OPS_HINTS.get(field, "Надішліть значення."),
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Скасувати", callback_data=f"vacops:{key}")]]),
        )

    if data.startswith("vacdel:"):
        key = data.split(":", 1)[1]
        return await edit(
            f"🗑 Видалити «{_vacancies.get(key).label}»?\n\n"
            "Кандидати, які вже прийшли з неї, залишаться — зникне лише сама вакансія.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Так, видалити", callback_data=f"vacdel2:{key}")],
                [InlineKeyboardButton("⬅️ Ні, назад", callback_data=f"vacops:{key}")],
            ]),
        )

    if data.startswith("vacdel2:"):
        key = data.split(":", 1)[1]
        try:
            _vstore.delete(key)
        except _vstore.VacancyStoreError as e:
            return await edit(f"❌ {e}", _vac_ops_kb(key))
        return await edit(_vac_list_text(), _vac_kb())

    # ---- paid resume search ----
    if data == "srch:pick":
        rows = [
            [InlineKeyboardButton(f"🔎 {v.label}", callback_data=f"srch:v:{k}")]
            for k, v in _vacancies.all_vacancies().items()
        ]
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="nav:main")])
        return await edit(
            "🔎 <b>Пошук резюме на work.ua</b>\n\n"
            "Єва сама шукає кандидатів у базі резюме, а не чекає відгуків.\n\n"
            "⚠️ <b>Це платно.</b> За кожного знайденого кандидата з бази "
            "списується відкриття контакту з вашого рахунку на work.ua.\n\n"
            "Під яку вакансію шукаємо?",
            InlineKeyboardMarkup(rows),
        )

    if data.startswith("srch:v:"):
        key = data.split(":", 2)[2]
        ctx.user_data["srch_vac"] = key
        ctx.user_data["await"] = "srch_query"
        return await edit(
            f"🔎 <b>{_vacancies.get(key).label}</b>\n\n"
            "Надішліть, кого шукаємо — як написали б у пошуку на сайті.\n"
            "Напр.: <code>менеджер з продажу логістика</code>\n\n"
            "Далі буде показано, скільки знайдено, і ви підтвердите обробку.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Скасувати", callback_data="nav:main")]]),
        )

    if data.startswith("srch:go:"):
        limit = int(data.split(":", 2)[2])
        key = ctx.user_data.get("srch_vac") or _vacancies.DEFAULT.key
        query = ctx.user_data.get("srch_query") or ""
        if not query:
            return await edit("❌ Запит загубився. Почніть спочатку.", _main_kb())
        await edit(f"🔎 Шукаю «{query}»… Це може зайняти хвилину.")
        from src.integrations.workua_sync import search_and_qualify

        vac = _vacancies.get(key)
        try:
            stats = await search_and_qualify(
                query=query,
                vacancy_id=_vacancies.LOCAL_FK,
                vacancy_text=f"{vac.label}. {_vstore.spoken(vac, 'spoken_benefits')}",
                vacancy_key=key,
                limit=limit,
            )
        except Exception as e:  # noqa: BLE001 — report, never crash the bot
            return await edit(f"❌ Пошук не вдався: {e}", _main_kb())
        return await edit(
            f"🔎 <b>Пошук завершено</b> — {vac.label}\n\n"
            f"Знайдено в базі: <b>{stats.get('found', 0)}</b>\n"
            f"➕ Додано в чергу: <b>{stats.get('accepted', 0)}</b>\n"
            f"↩️ Вже були в базі: {stats.get('duplicates', 0)}\n"
            f"🎯 Не пройшли фільтр: {stats.get('profile_rejected', 0)}\n"
            f"📉 Слабкий збіг: {stats.get('match_rejected', 0)}\n"
            f"⚠️ Помилок: {stats.get('errors', 0)}",
            _main_kb(),
        )

    if data == "vacnew":
        ctx.user_data["await"] = "vacnew_key"
        return await edit(
            "➕ <b>Нова вакансія</b>\n\n"
            "Надішліть короткий ключ латиницею — він потрібен системі, "
            "кандидати його не бачать.\n"
            "Напр.: <code>driver</code>, <code>hr_manager</code>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Скасувати", callback_data="nav:vac")]]),
        )
    if data == "nav:thr":
        return await edit("🎯 Оберіть поріг відбору:", _thr_kb())

    # set threshold
    if data.startswith("setthr:"):
        _apply_override("thr", data.split(":", 1)[1])
        return await edit(_crit_text(), _crit_kb())

    # toggle calls / status / report
    if data == "act:toggle_calls":
        _state["calls_paused"] = not calls_paused()
        _save_state()
        return await edit(_main_text(), _main_kb())
    if data == "act:trunk":
        keys = list(_TRUNKS)
        cur = _current_trunk_key()
        nxt = keys[(keys.index(cur) + 1) % len(keys)] if cur in keys else keys[0]
        _set_trunk(nxt)
        return await edit(_main_text(), _main_kb())
    if data == "act:status":
        from sqlalchemy import func, select

        from src.common import sources as _src
        from src.common.db import session_scope
        from src.common.models import Candidate

        s = get_settings()
        async with session_scope() as sess:
            rows = (await sess.execute(
                select(Candidate.source, func.count(Candidate.id))
                .group_by(Candidate.source)
            )).all()
        by_source: dict[str, int] = {}
        for raw, n in rows:
            lbl = _src.label(raw)
            by_source[lbl] = by_source.get(lbl, 0) + n
        src_txt = "\n".join(f"  • {k}: {v}" for k, v in sorted(by_source.items())) or "  —"
        txt = (
            "📊 <b>Статус</b>\n"
            f"Дзвінки: {'⏸ ПАУЗА' if calls_paused() else '🟢 активні'}\n"
            f"Поріг відбору: {s.match_score_threshold}\n"
            f"Агент: {s.agent_name} / {s.company_name}\n"
            f"\n<b>Кандидати за джерелом:</b>\n{src_txt}"
        )
        return await edit(txt, InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="nav:main")]]))
    if data == "act:report":
        from datetime import date
        from src.bot.report import format_report_md, collect_for as _c
        rep = await _c(date.today())
        await q.message.reply_text(format_report_md(rep), parse_mode=ParseMode.MARKDOWN)
        return

    # edit text-fields (criteria + vacancy)
    if data.startswith("ed:"):
        key = data.split(":", 1)[1]
        ctx.user_data["await"] = key
        prompt = _PROMPTS.get(key, "Надішліть нове значення.")
        back = "nav:crit" if key in ("rw", "rb", "agef", "agem") else "nav:vac"
        return await edit(f"✏️ {prompt}", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Скасувати", callback_data=back)]]))

    # ---- import candidates ----
    if data == "cand:add":
        ctx.user_data["await"] = "cand_paste"
        return await edit(
            "\U0001F4E5 <b>\u0414\u043E\u0434\u0430\u0442\u0438 \u043A\u0430\u043D\u0434\u0438\u0434\u0430\u0442\u0456\u0432</b>\n\n"
            "\u041D\u0430\u0434\u0456\u0448\u043B\u0456\u0442\u044C \u0441\u043F\u0438\u0441\u043E\u043A \u2014 \u043E\u0434\u0438\u043D \u043A\u0430\u043D\u0434\u0438\u0434\u0430\u0442 \u043D\u0430 \u0440\u044F\u0434\u043E\u043A:\n"
            "<code>+380671234567, \u041E\u043B\u0435\u0433, \u0414\u043D\u0456\u043F\u0440\u043E, \u043C\u0435\u043D\u0435\u0434\u0436\u0435\u0440</code>\n\n"
            "\u041D\u043E\u043C\u0435\u0440 \u043E\u0431\u043E\u0432\u2019\u044F\u0437\u043A\u043E\u0432\u0438\u0439, \u0440\u0435\u0448\u0442\u0430 \u2014 \u0437\u0430 \u0431\u0430\u0436\u0430\u043D\u043D\u044F\u043C. "
            "\u0410\u0431\u043E \u043D\u0430\u0434\u0456\u0448\u043B\u0456\u0442\u044C CSV-\u0444\u0430\u0439\u043B.",
            InlineKeyboardMarkup([[InlineKeyboardButton("\u2B05\uFE0F \u0421\u043A\u0430\u0441\u0443\u0432\u0430\u0442\u0438", callback_data="nav:main")]]))

    # ---- Telegram Eva ----
    if data == "nav:tg":
        ctx.user_data.pop("await", None)
        h = await _tg_get("/health")
        return await edit(await _tg_text(), _tg_kb(bool(h.get("active", True))))
    if data == "tg:toggle":
        await _tg_post("/toggle")
        h = await _tg_get("/health")
        return await edit(await _tg_text(), _tg_kb(bool(h.get("active", True))))
    if data == "tg:send":
        ctx.user_data["await"] = "tg_username"
        ctx.user_data["tgc"] = {}
        return await edit(
            "\u2709\uFE0F \u041D\u0430\u0434\u0456\u0448\u043B\u0456\u0442\u044C @username \u043A\u0430\u043D\u0434\u0438\u0434\u0430\u0442\u0430 (\u0430\u0431\u043E +380\u043D\u043E\u043C\u0435\u0440).",
            InlineKeyboardMarkup([[InlineKeyboardButton("\u2B05\uFE0F \u0421\u043A\u0430\u0441\u0443\u0432\u0430\u0442\u0438", callback_data="nav:tg")]]))

    # ---- test call wizard ----
    if data == "call:new":
        ctx.user_data["call"] = {}
        ctx.user_data["await"] = "call_phone"
        return await edit(
            "📞 <b>Тестовий дзвінок</b>\n\n" + _PROMPTS["call_phone"],
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Скасувати", callback_data="nav:main")]]),
        )
    if data.startswith("callf:"):
        field = data.split(":", 1)[1]  # name/position/region
        ctx.user_data["await"] = f"call_{field}"
        return await edit(f"✏️ {_PROMPTS['call_' + field]}",
                          InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="call:back")]]))
    if data == "call:back":
        cfg = ctx.user_data.get("call", {})
        return await edit(_call_card(cfg), _call_kb(cfg))
    if data == "call:cancel":
        ctx.user_data.pop("call", None)
        ctx.user_data.pop("await", None)
        return await edit(_main_text(), _main_kb())
    if data == "call:go":
        await _place_call(update, ctx, q)
        return


def _call_card(cfg: dict[str, Any]) -> str:
    return (
        "📞 <b>Тестовий дзвінок</b>\n\n"
        f"Номер: <code>{cfg.get('phone', '—')}</code>\n"
        f"Ім'я: {cfg.get('name') or '—'}\n"
        f"Посада: {cfg.get('position') or '—'}\n"
        f"Регіон: {cfg.get('region') or '—'}\n\n"
        "Заповніть за бажанням і натисніть «Подзвонити»."
    )


async def _place_call(update: Update, ctx: ContextTypes.DEFAULT_TYPE, q) -> None:
    cfg = ctx.user_data.get("call", {})
    phone = cfg.get("phone")
    if not phone:
        await q.edit_message_text("❌ Немає номера. Почніть заново.", reply_markup=_main_kb())
        return
    from src.call.script_template import render_system_prompt
    from src.call.vapi_client import VapiClient

    name = cfg.get("name") or "невідомий (тестовий дзвінок)"
    position = cfg.get("position") or "невідомо (запитати у кандидата)"
    region = cfg.get("region") or "невідомо (запитати у кандидата)"
    prompt = render_system_prompt(
        candidate_name=name, candidate_phone=phone,
        candidate_position=position, candidate_region=region, source="tg_menu",
    )
    overrides = {
        "model": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001",
                  "messages": [{"role": "system", "content": prompt}]},
        "variableValues": {"candidate_name": name, "candidate_position": position, "candidate_region": region},
    }
    vapi = VapiClient()
    try:
        res = await vapi.create_outbound_call(
            assistant_id=os.environ["VAPI_ASSISTANT_ID"],
            phone_number_id=os.environ["VAPI_PHONE_NUMBER_ID"],
            customer_number_e164=phone,
            assistant_overrides=overrides,
            metadata={"source": "tg_menu"},
        )
        cid = res.get("id", "?")
        await q.edit_message_text(
            f"📞 Дзвінок пішов!\ncall_id: <code>{cid}</code>\nномер: <code>{phone}</code>\n"
            f"кандидат: <b>{name}</b> / {position} / {region}",
            reply_markup=_main_kb(), parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await q.edit_message_text(
            f"❌ Помилка: <code>{type(e).__name__}: {str(e)[:250]}</code>",
            reply_markup=_main_kb(), parse_mode=ParseMode.HTML,
        )
    finally:
        await vapi.aclose()
        ctx.user_data.pop("call", None)
        ctx.user_data.pop("await", None)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture free-text only when the menu is waiting for input."""
    awaiting = ctx.user_data.get("await")
    if not awaiting or not _is_admin(update):
        return
    text = (update.message.text or "").strip()

    async def reply(t, kb=None):
        await update.message.reply_text(t, reply_markup=kb, parse_mode=ParseMode.HTML)

    # ---- criteria / vacancy edits ----
    if awaiting in ("rw", "rb"):
        _apply_override(awaiting, text)
        ctx.user_data.pop("await", None)
        return await reply("✅ Збережено.\n\n" + _crit_text(), _crit_kb())
    if awaiting in ("agef", "agem"):
        parts = text.replace("–", "-").replace(" ", "-").split("-")
        nums = [p for p in parts if p.isdigit()]
        if len(nums) < 2:
            return await reply("❌ Формат: <code>23-42</code>. Спробуйте ще раз.")
        lo, hi = nums[0], nums[1]
        _apply_override(f"{awaiting}_min", lo)
        _apply_override(f"{awaiting}_max", hi)
        ctx.user_data.pop("await", None)
        return await reply("✅ Збережено.\n\n" + _crit_text(), _crit_kb())
    if awaiting in ("vac_title", "vac_salary", "vac_schedule", "vac_benefits"):
        _apply_override(awaiting, text)
        ctx.user_data.pop("await", None)
        return await reply("✅ Збережено для всіх вакансій.\n\n" + _vac_text(), _vac_global_kb())

    if isinstance(awaiting, str) and awaiting.startswith("vacof:"):
        _, key, field = awaiting.split(":", 2)
        try:
            _vstore.set_field(key, field, text)
        except _vstore.VacancyStoreError as e:
            return await reply(f"❌ {e}\n\nСпробуйте ще раз.")
        ctx.user_data.pop("await", None)
        return await reply("✅ Збережено.\n\n" + _vac_ops_text(key), _vac_ops_kb(key))

    if awaiting == "srch_query":
        ctx.user_data.pop("await", None)
        ctx.user_data["srch_query"] = text
        key = ctx.user_data.get("srch_vac") or _vacancies.DEFAULT.key
        # Ask for the batch size instead of running straight away. Each accepted
        # candidate costs a contact opening, so the number has to be a deliberate
        # choice, not whatever the search happened to return.
        return await reply(
            f"🔎 <b>{_vacancies.get(key).label}</b>\nЗапит: <code>{text}</code>\n\n"
            "⚠️ Кожен доданий кандидат <b>витрачає відкриття контакту</b> на work.ua.\n\n"
            "Скільки резюме обробити за раз?",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("5 (обережно)", callback_data="srch:go:5"),
                 InlineKeyboardButton("10", callback_data="srch:go:10")],
                [InlineKeyboardButton("20", callback_data="srch:go:20")],
                [InlineKeyboardButton("⬅️ Скасувати", callback_data="nav:main")],
            ]),
        )

    if awaiting == "vacnew_key":
        try:
            _vstore.create(text, text)  # label is fixed on the next step
        except _vstore.VacancyStoreError as e:
            return await reply(f"❌ {e}\n\nСпробуйте ще раз.")
        ctx.user_data["await"] = f"vacof:{text.strip().lower()}:label"
        return await reply(
            "✅ Ключ прийнято.\n\nТепер надішліть назву, як вона має стояти в CRM.\n"
            "Напр.: <code>Водій категорії Е</code>"
        )

    if isinstance(awaiting, str) and awaiting.startswith("vacf:"):
        _, key, field = awaiting.split(":", 2)
        ctx.user_data.pop("await", None)
        try:
            _vstore.set_field(key, field, text)
        except ValueError as e:
            return await reply(f"❌ {e}")
        return await reply(
            f"✅ Збережено для «{_vacancies.get(key).label}».\n\n" + _vac_one_text(key),
            _vac_one_kb(key),
        )

    # ---- import candidates ----
    if awaiting == "cand_paste":
        ctx.user_data.pop("await", None)
        from src.common.import_candidates import import_from_lines
        r = await import_from_lines(text)
        msg = (f"\u2705 \u0414\u043E\u0434\u0430\u043D\u043E: <b>{r['added']}</b>\n"
               f"\u041F\u0440\u043E\u043F\u0443\u0449\u0435\u043D\u043E (\u0434\u0443\u0431\u043B\u0456): {r['skipped_dup']}\n"
               f"\u041D\u0435\u0432\u0456\u0440\u043D\u0438\u0439 \u043D\u043E\u043C\u0435\u0440: {r['skipped_bad']}")
        if r['bad_samples']:
            msg += "\n\u041F\u0440\u0438\u043A\u043B\u0430\u0434\u0438: <code>" + ", ".join(r['bad_samples']) + "</code>"
        if r['added']:
            msg += "\n\n\U0001F4DE \u0411\u0443\u0434\u0443\u0442\u044C \u043E\u0431\u0434\u0437\u0432\u043E\u043D\u0435\u043D\u0456 \u043D\u0430 \u043D\u0430\u0439\u0431\u043B\u0438\u0436\u0447\u043E\u043C\u0443 \u0441\u043B\u043E\u0442\u0456."
        return await reply(msg, _main_kb())

    # ---- Telegram Eva outreach ----
    if awaiting == "tg_username":
        ctx.user_data.setdefault("tgc", {})["target"] = text
        ctx.user_data["await"] = "tg_name"
        return await reply("\u0406\u043C\u2019\u044F \u043A\u0430\u043D\u0434\u0438\u0434\u0430\u0442\u0430 (\u0430\u0431\u043E \u00AB-\u00BB \u0431\u0435\u0437 \u0456\u043C\u0435\u043D\u0456):")
    if awaiting == "tg_name":
        tgc = ctx.user_data.get("tgc", {})
        name = "" if text == "-" else text
        ctx.user_data.pop("await", None)
        res = await _tg_post("/send", {"target": tgc.get("target", ""), "name": name})
        if res.get("ok"):
            tgt = tgc.get("target")
            st = res.get("sent_today")
            lim = res.get("limit")
            return await reply(f"\u2705 \u0404\u0432\u0430 \u043D\u0430\u043F\u0438\u0441\u0430\u043B\u0430 {tgt}. \u0421\u044C\u043E\u0433\u043E\u0434\u043D\u0456: {st}/{lim}", _tg_kb(True))
        err = res.get("error") or "\u043D\u0435\u0432\u0456\u0434\u043E\u043C\u0430 \u043F\u043E\u043C\u0438\u043B\u043A\u0430"
        return await reply(f"\u274C {err}", _tg_kb(True))

    # ---- test call wizard inputs ----
    if awaiting == "call_phone":
        if not text.startswith("+"):
            return await reply("❌ Номер має починатися з <code>+</code>. Напр.: <code>+380671234567</code>")
        ctx.user_data.setdefault("call", {})["phone"] = text
        ctx.user_data.pop("await", None)
        cfg = ctx.user_data["call"]
        return await reply(_call_card(cfg), _call_kb(cfg))
    if awaiting in ("call_name", "call_position", "call_region"):
        field = awaiting.split("_", 1)[1]
        ctx.user_data.setdefault("call", {})[field] = text
        ctx.user_data.pop("await", None)
        cfg = ctx.user_data["call"]
        return await reply(_call_card(cfg), _call_kb(cfg))


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Import candidates from an uploaded CSV/TXT file."""
    if not _is_admin(update):
        return
    doc = update.message.document
    if not doc:
        return
    name = (doc.file_name or "").lower()
    if not (name.endswith(".csv") or name.endswith(".txt")):
        await update.message.reply_text("\u041D\u0430\u0434\u0456\u0448\u043B\u0456\u0442\u044C .csv \u0430\u0431\u043E .txt \u0444\u0430\u0439\u043B.")
        return
    f = await doc.get_file()
    raw = await f.download_as_bytearray()
    try:
        text = bytes(raw).decode("utf-8-sig")
    except Exception:
        text = bytes(raw).decode("cp1251", errors="ignore")
    from src.common.import_candidates import import_from_lines
    r = await import_from_lines(text)
    await update.message.reply_text(
        f"\U0001F4E5 \u0406\u043C\u043F\u043E\u0440\u0442 \u0437 \u0444\u0430\u0439\u043B\u0443:\n"
        f"\u2705 \u0414\u043E\u0434\u0430\u043D\u043E: <b>{r['added']}</b> | \u0434\u0443\u0431\u043B\u0456: {r['skipped_dup']} | \u043F\u043E\u043C\u0438\u043B\u043A\u0438: {r['skipped_bad']}",
        reply_markup=_main_kb(), parse_mode=ParseMode.HTML)


def register_menu_handlers(app) -> None:
    apply_saved_overrides()
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CallbackQueryHandler(on_callback))
    # text capture runs in a later group so it never shadows command handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=1)
    app.add_handler(MessageHandler(filters.Document.ALL, on_document), group=1)
