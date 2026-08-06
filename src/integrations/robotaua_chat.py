"""robota.ua cabinet chat → intake.

The free route to candidates whose phone robota.ua hides behind the billable
"open contacts" action: they already wrote to the company in the cabinet chat,
and their number is reachable there in two ways —

  1. phone-signup users carry it in the CV e-mail robota.ua generates for them:
     `380930206874@phone-registration.rabota.ua`;
  2. people routinely type a number into the chat itself.

Both are free. Everything here is read-only: `send_message()` exists on the
client, but this module only calls it when ROBOTAUA_CHAT_REPLY_ENABLED is
explicitly on — messaging real people on the client's behalf is theirs to
authorise, not ours to assume.

Payload shapes are the cabinet's own (verified live 2026-08-03):
    GET /v2/conversations/all -> {"data": [conversation, ...]}
    conversation: conversationId, conversationName, contextName, oppositeUserId,
                  unreadMessagesCount, isArchived, lastMessage, messages[],
                  cv{resumeId, fullName, name, surname, email, cityId,
                     speciality, age, isOpenResumeInfo},
                  vacancy{vacancyId, vacancyName}
    message:      id, created (epoch ms), owned (true = sent by us), seen,
                  messageType (Text|Apply|Cv|Vacancy), text

Conversations carry their recent messages inline, so a poll is normally ONE
request — which matters, because Cloudflare challenges this host's addresses
whenever traffic looks bursty.

Env:
    ROBOTAUA_CHAT_ENABLED           1 = poller runs (default 1)
    ROBOTAUA_CHAT_MAX_CONVERSATIONS conversations inspected per poll (default 8)
    ROBOTAUA_CHAT_MAX_AGE_DAYS      ignore threads older than N days (default 60)
    ROBOTAUA_CHAT_VACANCY_FILTER    1 = restrict to ROBOTAUA_ALLOWED_VACANCY_IDS (default 0)
    ROBOTAUA_CHAT_REPLY_ENABLED     1 = Єва asks the candidate for a phone (default 0)
    ROBOTAUA_CHAT_REPLIES_PER_RUN   cap per poll (default 2)
    ROBOTAUA_CHAT_REPLIES_PER_DAY   cap per day (default 12)
    ROBOTAUA_ALLOWED_VACANCY_IDS    shared with the responses poller
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog

from src.api.inbound_router import IngestPayload, InboundRouter
from src.common.phone import normalize_phone
from src.common.settings import get_settings
from src.integrations.robotaua_api import (
    CANDIDATE_URL,
    RobotaUaBlockedError,
    RobotaUaClient,
    RobotaUaError,
    parse_add_date,
    state_dir,
    write_status,
)
from src.common import vacancies
from src.integrations.robotaua_sync import allowed_vacancy_ids
from src.match.profile_filter import evaluate as profile_evaluate

log = structlog.get_logger()

SOURCE = "robotaua_chat"
CURSOR_NAME = "robotaua_chat_cursor.json"
REQUEST_PAUSE_SEC = 1.5

# robota.ua's generated address for phone-signup users — an exact, free phone.
PHONE_MAIL_RE = re.compile(r"^(\d{10,12})@phone-registration\.rabota\.ua$", re.I)
# Ukrainian numbers as people type them in chat: 0671234567, +380671234567,
# 380 67 123 45 67, (067) 123-45-67 …
PHONE_TEXT_RE = re.compile(
    r"(?:\+?38[\s\-()]*)?0\s*\d{2}[\s\-()]*\d{3}[\s\-()]*\d{2}[\s\-()]*\d{2}"
)


# Approved by the client 2026-08-03. Persona rules carried over from the Telegram
# Єва: feminine self-reference, no surname, never names the screening criteria,
# never hands out the recruiter's number.
REPLY_PLAIN = (
    "Доброго дня! Мене звати Єва, я рекрутерка компанії Козир Транс — ми займаємось "
    "організацією внутрішніх та міжнародних вантажоперевезень. Дякую за ваш відгук на "
    "вакансію менеджера з логістики. Робота повністю віддалена, 5 днів на тиждень "
    "з 9:00 до 17:00, дохід від 30 тисяч гривень.\n"
    "Підкажіть, будь ласка, ваш номер телефону — зателефоную, коротко розкажу деталі "
    "й відповім на питання."
)
REPLY_ASKED = (
    "Доброго дня! Мене звати Єва, я рекрутерка компанії Козир Транс. Так, вакансія "
    "актуальна — шукаємо менеджера з логістики, робота повністю віддалена, 5 днів на "
    "тиждень з 9:00 до 17:00, дохід від 30 тисяч гривень.\n"
    "Залиште, будь ласка, ваш номер телефону — зателефоную й розкажу деталі."
)
# Threads that sat unanswered for months, often about a different vacancy — so
# this one apologises for the silence and does NOT claim what they applied to.
REPLY_STALE = (
    "Доброго дня! Мене звати Єва, я рекрутерка компанії Козир Транс. Перепрошую за "
    "довгу паузу з нашого боку. Зараз у нас відкрита вакансія менеджера з логістики: "
    "робота повністю віддалена, 5 днів на тиждень з 9:00 до 17:00, дохід від 30 тисяч "
    "гривень.\n"
    "Якщо вам це актуально — залиште, будь ласка, ваш номер телефону, і я зателефоную "
    "з деталями."
)
# How old a thread has to be before it gets the apology wording instead.
STALE_REPLY_AFTER_DAYS = 30


@dataclass
class ChatPollStats:
    conversations: int = 0
    unread: int = 0
    scanned: int = 0
    todo: int = 0
    phones_from_email: int = 0
    phones_from_text: int = 0
    no_phone: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    profile_rejected: int = 0
    replies_sent: int = 0
    errors: int = 0
    blocked: bool = False
    samples: list[str] = field(default_factory=list)

    def as_log(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "samples"}


def _env_flag(name: str, default: str = "1") -> bool:
    return (os.getenv(name) or default).strip().lower() not in ("0", "false", "no", "off", "")


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def _cursor_path():
    return state_dir() / CURSOR_NAME


def load_cursor() -> dict:
    try:
        data = json.loads(_cursor_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cursor(cursor: dict) -> None:
    try:
        path = _cursor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cursor, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.error("robotaua_chat.cursor_save_failed", error=str(e))


def phone_from_email(email: str | None) -> str | None:
    match = PHONE_MAIL_RE.match((email or "").strip())
    return normalize_phone(match.group(1)) if match else None


def phone_from_texts(texts: list[str]) -> str | None:
    for text in texts:
        for raw in PHONE_TEXT_RE.findall(text or ""):
            phone = normalize_phone(raw)
            if phone:
                return phone
    return None


def candidate_texts(messages: list[dict]) -> list[str]:
    """Only what the candidate wrote. `owned` messages are ours — a number in
    one of those is the company's own line, not theirs."""
    return [
        str(m.get("text") or "")
        for m in messages
        if isinstance(m, dict) and not m.get("owned") and m.get("text")
    ]


def transcript_of(messages: list[dict], limit: int = 4000) -> str:
    lines = []
    for m in messages:
        if not isinstance(m, dict) or not m.get("text"):
            continue
        who = "Єва/рекрутер" if m.get("owned") else "Кандидат"
        lines.append(f"{who}: {m['text']}")
    return "\n".join(lines)[:limit]


async def poll_chats(
    *,
    client: RobotaUaClient | None = None,
    router: InboundRouter | None = None,
    dry_run: bool = False,
) -> ChatPollStats:
    stats = ChatPollStats()
    if not _env_flag("ROBOTAUA_CHAT_ENABLED"):
        return stats

    cursor = load_cursor()
    blocked_until = parse_add_date(cursor.get("blocked_until"))
    if blocked_until and datetime.utcnow() < blocked_until:
        log.info("robotaua_chat.cooldown", until=cursor.get("blocked_until"))
        write_status(chat_blocked_until=cursor.get("blocked_until"))
        return stats

    client = client or RobotaUaClient()
    router = router or InboundRouter()
    handled: dict[str, dict] = dict(cursor.get("conversations") or {})
    max_conversations = _env_int("ROBOTAUA_CHAT_MAX_CONVERSATIONS", 8)
    # calls_only: Єва works the sales vacancies. An intake-only applicant
    # («Бухгалтер») must never get a message from her — the recruiter handles
    # that thread by hand in the cabinet.
    allowed = allowed_vacancy_ids(calls_only=True)

    try:
        # Params are the cabinet's own: without an explicit fetchType the API
        # answers with the *archived* folder (verified 2026-08-03), which is the
        # 2024 dead pile — "Regular" is the live one recruiters look at.
        payload = await client.list_conversations(
            pageSize=_env_int("ROBOTAUA_CHAT_PAGE_SIZE", 20),
            pageNumber=1,
            fetchType="Regular",
        )
    except RobotaUaBlockedError as e:
        stats.blocked = True
        stats.errors += 1
        _cooldown(cursor)
        log.warning("robotaua_chat.blocked", error=str(e))
        return stats
    except RobotaUaError as e:
        stats.errors += 1
        log.error("robotaua_chat.list_failed", error=str(e))
        return stats

    conversations = payload.get("data") if isinstance(payload, dict) else payload
    conversations = [c for c in (conversations or []) if isinstance(c, dict)]
    stats.conversations = len(conversations)
    # Unread that actually needs a human/Єва: robota.ua keeps counting a thread as
    # unread even after we answered (we never mark it read on their side), so the
    # honest number is "threads where the candidate wrote last".
    unread_total = sum(
        int(c.get("unreadMessagesCount") or 0)
        for c in conversations
        if not ((c.get("lastMessage") or {}).get("owned"))
    )

    try:
        cities = await client.city_map()
    except RobotaUaError:
        cities = {}

    # Chats span every vacancy the company ever posted, so the response poller's
    # allowlist is off by default here — recency is the better gate, and the
    # profile/geo filters still decide who actually enters the funnel.
    use_vacancy_filter = _env_flag("ROBOTAUA_CHAT_VACANCY_FILTER", "0")
    max_age_days = _env_int("ROBOTAUA_CHAT_MAX_AGE_DAYS", 60)
    oldest_allowed = datetime.utcnow() - timedelta(days=max_age_days)

    # Build the work list BEFORE applying the per-run cap. Capping first meant
    # the same already-handled threads filled the slice on every poll and the
    # rest of the backlog was never reached.
    todo: list[tuple[dict, str]] = []
    for conv in conversations:
        conv_id = str(conv.get("conversationId") or "")
        if not conv_id:
            continue
        vacancy_id = (conv.get("vacancy") or {}).get("vacancyId")
        if use_vacancy_filter and allowed and vacancy_id and vacancy_id not in allowed:
            continue
        # Unconditional, and deliberately not behind ROBOTAUA_CHAT_VACANCY_FILTER
        # (which is off by default): an intake-only vacancy is worked by a human,
        # so Єва must not open a conversation there no matter how the allowlist
        # is configured.
        _route = vacancies.for_robotaua(vacancy_id)
        if _route is not None and not _route.calls_enabled:
            continue

        last_message = conv.get("lastMessage") if isinstance(conv.get("lastMessage"), dict) else {}
        created_ms = last_message.get("created")
        if isinstance(created_ms, (int, float)):
            if datetime.utcfromtimestamp(created_ms / 1000) < oldest_allowed:
                continue  # stale thread — the person moved on months ago
        signature = str(last_message.get("id") or created_ms or "")
        prev = handled.get(conv_id) or {}
        if prev.get("ingested"):
            continue  # already in the funnel
        if signature and prev.get("signature") == signature:
            continue  # nothing new since we last looked
        todo.append((conv, signature))

    # Unread first: someone waiting on an answer beats an old quiet thread.
    todo.sort(key=lambda item: int(item[0].get("unreadMessagesCount") or 0), reverse=True)

    stats.todo = len(todo)
    for conv, signature in todo[:max_conversations]:
        conv_id = str(conv.get("conversationId") or "")
        cv = conv.get("cv") or {}
        vacancy_id = (conv.get("vacancy") or {}).get("vacancyId")
        prev = handled.get(conv_id) or {}

        stats.unread += int(conv.get("unreadMessagesCount") or 0)
        stats.scanned += 1

        messages = [m for m in (conv.get("messages") or []) if isinstance(m, dict)]
        if not messages:
            # Older conversations come back without their message list inline.
            await asyncio.sleep(REQUEST_PAUSE_SEC)
            try:
                # Without explicit paging the endpoint can answer with a stale
                # page that misses the newest messages (seen 2026-08-03).
                fetched = await client.get_messages(conv_id, pageSize=20, pageNumber=1)
            except RobotaUaBlockedError as e:
                stats.blocked = True
                log.warning("robotaua_chat.blocked_mid_poll", conversation=conv_id, error=str(e))
                break
            except RobotaUaError as e:
                stats.errors += 1
                log.warning("robotaua_chat.messages_failed", conversation=conv_id, error=str(e))
                continue
            rows = fetched.get("data") if isinstance(fetched, dict) else fetched
            messages = [m for m in (rows or []) if isinstance(m, dict)]

        phone = phone_from_email(cv.get("email"))
        if phone:
            stats.phones_from_email += 1
        else:
            phone = phone_from_texts(candidate_texts(messages))
            if phone:
                stats.phones_from_text += 1

        name = str(
            cv.get("fullName")
            or conv.get("conversationName")
            or "Кандидат robota.ua"
        ).strip()
        entry = {
            "signature": signature,
            "name": name,
            "seen_at": datetime.utcnow().isoformat(timespec="seconds"),
            "messages": len(messages),
            "unread": int(conv.get("unreadMessagesCount") or 0),
            "has_phone": bool(phone),
            "resume_id": cv.get("resumeId"),
            "vacancy_id": vacancy_id,
        }

        if not phone:
            stats.no_phone += 1
            entry["replied_at"] = prev.get("replied_at")
            if await _maybe_reply(client, conv_id, messages, entry, cursor, stats, dry=dry_run):
                entry["replied_at"] = datetime.utcnow().isoformat(timespec="seconds")
            handled[conv_id] = entry
            continue

        geo = cities.get(int(cv["cityId"])) if cv.get("cityId") else None
        age = cv.get("age")
        birth_year = None
        try:
            birth_year = datetime.utcnow().year - int(age) if age else None
        except (TypeError, ValueError):
            birth_year = None

        transcript = transcript_of(messages)
        # A chat thread is a few lines, not a CV, so the role check has nothing to
        # bite on and would reject people who literally applied to this vacancy.
        # The position they applied for IS the vacancy — say so, and let the geo
        # and age gates do the filtering. Єва re-screens everything on the call.
        profile = profile_evaluate(
            full_name=name,
            region=(geo or {}).get("region"),
            desired_position=cv.get("speciality") or get_settings().keycrm_vacancy_label,
            resume_text=transcript,
            experience_text=transcript,
            birth_year=birth_year,
        )
        if not profile.accepted:
            stats.profile_rejected += 1
            log.info(
                "robotaua_chat.profile_rejected",
                conversation=conv_id,
                name=name,
                reason=profile.reason,
            )
            entry["ingested"] = True  # decided — no need to look again
            handled[conv_id] = entry
            continue

        if dry_run:
            stats.samples.append(f"{name} | {phone} | {(geo or {}).get('city')} | {len(messages)} msgs")
            handled[conv_id] = entry
            continue

        result = await router.ingest(
            IngestPayload(
                full_name=name,
                phone_raw=phone,
                email=None if phone_from_email(cv.get("email")) else (cv.get("email") or None),
                region_raw=(geo or {}).get("region"),
                desired_position=cv.get("speciality"),
                work_ua_url=(
                    CANDIDATE_URL.format(resume_id=cv["resumeId"]) if cv.get("resumeId") else None
                ),
                resume_text=f"Переписка в чаті robota.ua:\n{transcript}" if transcript else None,
                source=SOURCE,
                vacancy_id=vacancies.LOCAL_FK,
                vacancy_key=(
                    vacancies.for_robotaua(vacancy_id) or vacancies.DEFAULT
                ).key,
            )
        )
        if not result.accepted:
            stats.rejected += 1
            log.info("robotaua_chat.ingest_rejected", conversation=conv_id, reason=result.reason)
        elif result.duplicate:
            stats.duplicates += 1
        else:
            stats.accepted += 1
            log.info(
                "robotaua_chat.ingested",
                conversation=conv_id,
                candidate=result.candidate_id,
                name=name,
                phone_source="email" if phone_from_email(cv.get("email")) else "text",
            )
        entry["ingested"] = True
        handled[conv_id] = entry

    cursor["conversations"] = handled
    cursor["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
    if stats.blocked:
        _cooldown(cursor)
    elif not dry_run:
        cursor.pop("blocked_until", None)
        save_cursor(cursor)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    write_status(
        chat_unread=unread_total,
        # Threads still waiting on us — the number a human should act on.
        chat_todo=stats.todo,
        chat_conversations=stats.conversations,
        chat_replies_today=(cursor.get("replies_per_day") or {}).get(today, 0),
        chat_last_ok=(
            None if stats.blocked else datetime.utcnow().isoformat(timespec="seconds")
        ),
        chat_blocked_until=cursor.get("blocked_until"),
    )
    log.info("robotaua_chat.poll.done", **stats.as_log())
    return stats


async def _maybe_reply(
    client: RobotaUaClient,
    conv_id: str,
    messages: list[dict],
    entry: dict,
    cursor: dict,
    stats: ChatPollStats,
    *,
    dry: bool,
) -> bool:
    """Ask a candidate for their number — the only free way to reach the people
    whose contacts robota.ua keeps hidden.

    Sends at most once per conversation, with a per-run and per-day cap so a bug
    can never turn into a mass mailing. Off unless ROBOTAUA_CHAT_REPLY_ENABLED=1.
    """
    if not _env_flag("ROBOTAUA_CHAT_REPLY_ENABLED", "0"):
        return False
    if entry.get("replied_at"):
        return False
    if stats.replies_sent >= _env_int("ROBOTAUA_CHAT_REPLIES_PER_RUN", 2):
        return False

    today = datetime.utcnow().strftime("%Y-%m-%d")
    counters = dict(cursor.get("replies_per_day") or {})
    if counters.get(today, 0) >= _env_int("ROBOTAUA_CHAT_REPLIES_PER_DAY", 12):
        log.info("robotaua_chat.daily_reply_cap_reached", day=today)
        return False

    # Someone who wrote real text gets the answer to their question; a bare
    # "applied" event gets the plain intro; anything that has been sitting for
    # over a month gets the apology version, which never claims which vacancy
    # they applied to (many of those threads are about older postings).
    asked = any(
        m.get("messageType") == "Text" and not m.get("owned") and (m.get("text") or "").strip()
        for m in messages
    )
    newest = max(
        (m.get("created") or 0 for m in messages if isinstance(m.get("created"), (int, float))),
        default=0,
    )
    stale = bool(newest) and (
        datetime.utcnow() - datetime.utcfromtimestamp(newest / 1000)
    ) > timedelta(days=STALE_REPLY_AFTER_DAYS)
    if stale:
        text, variant = REPLY_STALE, "stale"
    elif asked:
        text, variant = REPLY_ASKED, "asked"
    else:
        text, variant = REPLY_PLAIN, "plain"

    if dry:
        stats.samples.append(f"WOULD REPLY [{variant}] -> {entry.get('name')}")
        return False

    try:
        await client.send_message(conv_id, text)
    except RobotaUaBlockedError:
        stats.blocked = True
        log.warning("robotaua_chat.blocked_on_reply", conversation=conv_id)
        return False
    except RobotaUaError as e:
        stats.errors += 1
        log.warning("robotaua_chat.reply_failed", conversation=conv_id, error=str(e))
        return False

    stats.replies_sent += 1
    counters[today] = counters.get(today, 0) + 1
    cursor["replies_per_day"] = counters
    log.info(
        "robotaua_chat.replied",
        conversation=conv_id,
        name=entry.get("name"),
        variant=variant,
    )
    await asyncio.sleep(REQUEST_PAUSE_SEC)
    return True


def _cooldown(cursor: dict) -> None:
    minutes = _env_int("ROBOTAUA_BLOCK_COOLDOWN_MIN", 30)
    cursor["blocked_until"] = (
        datetime.utcnow() + timedelta(minutes=minutes)
    ).isoformat(timespec="seconds")
    save_cursor(cursor)
    log.warning("robotaua_chat.cooldown_started", minutes=minutes)
