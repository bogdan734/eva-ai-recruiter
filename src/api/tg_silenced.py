"""Tell a human when Eva has to stay quiet in Telegram.

Once a recruiter owns a candidate's card, /internal/tg-gate tells Eva not to
answer. Until 28.09 nothing else happened: the reply sat in Eva's chats, which no
recruiter reads, and all three people who answered the September outreach were
left waiting there. The userbot now reports such a reply here, and this module
passes it to the admins' bot chat and notes it on the card.
"""
from __future__ import annotations

from html import escape

import structlog
from sqlalchemy import select

from src.call.line_health import alert_admins
from src.common.crm import get_crm
from src.common.db import session_scope
from src.common.models import Candidate
from src.common.phone import normalize_phone

log = structlog.get_logger()

_STATUS_UA = {
    "manager_review": "у рекрутера",
    "interview_scheduled": "запрошено на співбесіду",
    "closed": "закрито",
}


def alert_text(
    *,
    name: str,
    username: str | None,
    phone: str | None,
    status: str | None,
    lead_id: int | None,
    card: str | None,
    messages: list[str],
) -> str:
    """The admins' message. `card` is "alive", "deleted" or None when unknown.

    Everything that came from the candidate is escaped: the bot sends HTML, and
    one stray "<" would make Telegram reject the whole alert.
    """
    who = [f"<b>{escape(name or 'Без імені')}</b>"]
    if phone:
        who.append(escape(phone))
    if username:
        who.append(escape(f"@{username}"))

    status = str(getattr(status, "value", status) or "")
    if not lead_id:
        where = "Картки немає" if status else "Кандидата немає в базі"
    elif card == "deleted":
        where = f"Картку #{lead_id} видалено в CRM"
    else:
        where = f"Картка #{lead_id} · {escape(_STATUS_UA.get(status, status or '?'))}"

    lines = [
        "💬 <b>Кандидат написав Єві в Telegram — Єва не відповідає</b>",
        " · ".join(who),
        where,
        "",
        *(f"«{escape(m)}»" for m in messages),
        "",
        "Єва мовчить, бо картка вже в рекрутера. Відповісти можна з акаунта Єви "
        "в Telegram або подзвонити.",
    ]
    return "\n".join(lines)


async def handle_tg_silenced(
    *,
    peer_id: str,
    name: str,
    username: str | None,
    phone: str | None,
    messages: list[str],
    crm=None,
) -> dict:
    """Alert the admins and note the reply on the card. ok=False when no admin chat
    accepted the alert, so the userbot tries again with the next sweep."""
    keys = [f"tg{peer_id}"[:20]]
    norm = None
    if phone:
        try:
            norm = normalize_phone(phone)
        except Exception:  # noqa: BLE001
            norm = None
        if norm:
            keys.insert(0, norm)
    async with session_scope() as session:
        cand = (await session.execute(
            select(Candidate).where(Candidate.phone_e164.in_(keys))
        )).scalars().first()
    lead_id = cand.keycrm_lead_id if cand else None
    said = [m.strip()[:500] for m in messages if m and m.strip()][-5:]

    kc = crm or get_crm()
    try:
        card = None
        if lead_id:
            try:
                alive = await kc.card_pipeline(int(lead_id)) is not None
                card = "alive" if alive else "deleted"
            except Exception as e:  # noqa: BLE001 — the alert matters more than the label
                log.warning("tg.silenced_card_lookup_failed", lead_id=lead_id, error=str(e))

        real = cand is not None and cand.phone_e164.startswith("+")
        shown_phone = cand.phone_e164 if real else (norm or phone)
        delivered = await alert_admins(alert_text(
            name=(cand.full_name if cand else "") or name,
            username=username,
            phone=shown_phone,
            status=cand.status if cand else None,
            lead_id=lead_id,
            card=card,
            messages=said,
        ))

        if card == "alive":
            try:
                await kc.append_manager_comment(
                    int(lead_id),
                    "Telegram: кандидат написав Єві, Єва не відповідає (картка в рекрутера): "
                    + " / ".join(f"«{m}»" for m in said),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("tg.silenced_comment_failed", lead_id=lead_id, error=str(e))
    finally:
        if crm is None:
            await kc.aclose()

    log.info("tg.silenced_forwarded", peer=peer_id, candidate_id=cand.id if cand else None,
             lead_id=lead_id, card=card, delivered=delivered)
    return {"ok": delivered > 0, "delivered": delivered}
