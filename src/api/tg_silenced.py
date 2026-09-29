"""Tell a recruiter when a candidate they own writes to Eva in Telegram.

Eva leads every Telegram dialog herself (src/api/tg_gate.py) until a recruiter
has the card. From then on she stays quiet — and the candidate's message must
reach the recruiter instead of sitting in Eva's chats, which nobody reads. A
card that was already dispositioned (Не ЦА, Не актуально, …) pings nobody.
"""
from __future__ import annotations

from html import escape as _escape

import structlog

from src.api.tg_gate import decide, find_candidate
from src.call.line_health import alert_admins
from src.common.crm import get_crm

log = structlog.get_logger()


def escape(text: str) -> str:
    """Telegram HTML needs only <, > and & escaped; an apostrophe must stay one."""
    return _escape(text, quote=False)


_STAGE_UA = {
    2: "Відібрано",
    3: "В роботі",
    4: "Дійшов на 1 тур",
    10: "Запросили на 1 тур",
    30: "Підтвердили участь",
}


def alert_text(
    *,
    name: str,
    username: str | None,
    phone: str | None,
    lead_id: int | None,
    stage: int | None,
    messages: list[str],
    paused: bool = False,
) -> str:
    """The recruiters' message. Everything from the candidate is escaped: the bot
    sends HTML, and one stray "<" would make Telegram reject the whole alert."""
    who = [f"<b>{escape(name or 'Без імені')}</b>"]
    if phone:
        who.append(escape(phone))
    if username:
        who.append(escape(f"@{username}"))
    if paused:
        return "\n".join([
            "💬 <b>Кандидат написав Єві в Telegram — Єва на паузі</b>",
            " · ".join(who),
            *([f"Картка #{lead_id}"] if lead_id else []),
            "",
            *(f"«{escape(m)}»" for m in messages),
            "",
            "Єва зараз зупинена й не відповідає. Відповісти можна з акаунта Єви "
            "в Telegram або подзвонити.",
        ])
    where = (f"Картка #{lead_id} · етап «{_STAGE_UA.get(stage, stage)}»" if stage
             else f"Картка #{lead_id} · етап не вдалося перевірити в CRM")
    return "\n".join([
        "💬 <b>Ваш кандидат написав Єві в Telegram</b>",
        " · ".join(who),
        where,
        "",
        *(f"«{escape(m)}»" for m in messages),
        "",
        "Картка вже у вас, тому Єва не відповідає. Відповісти можна з акаунта Єви "
        "в Telegram або подзвонити.",
    ])


async def handle_tg_silenced(
    *,
    peer_id: str,
    name: str,
    username: str | None,
    phone: str | None,
    messages: list[str],
    crm=None,
) -> dict:
    """Alert the recruiters when they own the card; otherwise record why not.
    ok=False only when an alert was due and no admin chat accepted it, so the
    userbot tries again with the next message or sweep."""
    cand = await find_candidate(peer_id, phone)
    said = [m.strip()[:500] for m in messages if m and m.strip()][-5:]
    kc = crm or get_crm()
    try:
        d = await decide(cand, kc)
        if not d.notify:
            log.info("tg.silenced_not_forwarded", peer=peer_id, why=d.why)
            return {"ok": True, "delivered": 0, "skipped": d.why}

        lead_id = cand.keycrm_lead_id if cand else None
        real = cand is not None and cand.phone_e164.startswith("+")
        delivered = await alert_admins(alert_text(
            name=(cand.full_name if cand else "") or name,
            username=username,
            phone=cand.phone_e164 if real else phone,
            lead_id=lead_id,
            stage=d.stage,
            messages=said,
            paused=d.why == "paused",
        ))
        if lead_id and d.stage:
            try:
                await kc.append_manager_comment(
                    int(lead_id),
                    "Telegram: кандидат написав Єві, картка в рекрутера: "
                    + " / ".join(f"«{m}»" for m in said),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("tg.silenced_comment_failed", lead_id=lead_id, error=str(e))
    finally:
        if crm is None:
            await kc.aclose()

    log.info("tg.silenced_forwarded", peer=peer_id, lead_id=lead_id, stage=d.stage,
             delivered=delivered)
    return {"ok": delivered > 0, "delivered": delivered}
