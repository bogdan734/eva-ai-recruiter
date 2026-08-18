#!/usr/bin/env python3
"""Telegram userbot «Єва» — сервіс для VPS.

Запускає одночасно:
  1. listen — авто-відповіді кандидатам (Claude, персона Єви)
  2. HTTP control API на :8090 — керування з адмін-панелі (Telegram-бот):
     GET  /health            -> {ok, me, active, sent_today, limit}
     GET  /stats             -> {sent_today, limit, active}
     POST /send  {target,name} -> надіслати інтро кандидату
     POST /toggle            -> увімк/вимк активність (пауза)
     POST /limit {value}     -> денний ліміт нових діалогів
"""
import asyncio
import json
import os
import random

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, PeerFloodError, UserPrivacyRestrictedError
from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
from telethon.tl.types import InputPhoneContact
from anthropic import Anthropic

import store
from persona import SYSTEM_PROMPT as _BASE_PROMPT, INTRO_TEMPLATE

load_dotenv()
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
PHONE = os.environ["TG_PHONE"]
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CTRL_PORT = int(os.environ.get("TG_CONTROL_PORT", "8090"))
STATE_PATH = os.environ.get("TG_STATE_PATH", "eva_state.json")

client = TelegramClient(os.environ.get("TG_SESSION", "eva_session"), API_ID, API_HASH)
claude = Anthropic()

VACANCY_URL = os.environ.get("VACANCY_URL", "https://www.work.ua/jobs/8249916/")
API_URL = os.environ.get("API_URL", "http://api:8000")
INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "change-me-internal")
SYSTEM_PROMPT = _BASE_PROMPT.replace("{VACANCY_URL}", VACANCY_URL)

# Debounce: coalesce a burst of rapid messages from one peer into a single reply.
TG_DEBOUNCE_SEC = float(os.environ.get("TG_DEBOUNCE_SEC", "6"))
TG_CATCHUP_ON_START = os.environ.get("TG_CATCHUP_ON_START", "1") not in ("0", "false", "no")
# Bot admins (recruiters) share this Telegram account's inbox. They are colleagues,
# not candidates — Eva must never run the screening script at them.
TG_ADMIN_PEERS = {p.strip() for p in os.environ.get("TG_ADMIN_CHAT_IDS", "").split(",") if p.strip()}
_peer_seq: dict[str, int] = {}


# ------------------------------ runtime state ------------------------------
def _load_state() -> dict:
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {"active": True, "limit": int(os.environ.get("MAX_NEW_CONTACTS_PER_DAY", 15))}


def _save_state(st: dict) -> None:
    try:
        json.dump(st, open(STATE_PATH, "w"))
    except Exception:
        pass


STATE = _load_state()


async def human_typing(peer, text: str):
    async with client.action(peer, "typing"):
        await asyncio.sleep(min(2 + len(text) * 0.06, 12) * random.uniform(0.8, 1.2))


# ------------------------------ core actions ------------------------------
ANKETA_FORM_URL = os.environ.get("ANKETA_FORM_URL", "https://forms.gle/AKCs5pAmDpKw1SEKA")
FORM_TEMPLATE = (
    "Доброго дня! Це Єва з компанії Kozyr Trans — ми щойно спілкувалися телефоном. "
    "Як і домовлялися, надсилаю анкету кандидата: {link}\n"
    "Заповніть, будь ласка, — після розгляду наш рекрутер зв\u2019яжеться з вами."
)


async def do_send_form(phone: str, name: str) -> dict:
    phone = phone.strip()
    if not phone:
        return {"ok": False, "error": "no phone"}
    if not phone.startswith("+"):
        phone = "+" + phone
    entity = None
    imported = False
    try:
        entity = await client.get_entity(phone)
    except Exception:
        entity = None
    if entity is None:
        try:
            res = await client(ImportContactsRequest(contacts=[
                InputPhoneContact(client_id=0, phone=phone,
                                  first_name=name or "Кандидат", last_name="")]))
            if res.users:
                entity = res.users[0]
                imported = True
        except Exception as e:
            return {"ok": False, "error": f"import failed: {e}"}
    if entity is None:
        return {"ok": False, "error": "номер не в Telegram або приховує телефон"}
    text = FORM_TEMPLATE.format(link=ANKETA_FORM_URL)
    try:
        await human_typing(entity, text)
        await client.send_message(entity, text)
    except PeerFloodError:
        STATE["active"] = False
        _save_state(STATE)
        return {"ok": False, "error": "PeerFloodError — акаунт обмежено, розсилку зупинено"}
    except UserPrivacyRestrictedError:
        return {"ok": False, "error": "приватність: не приймає повідомлення"}
    except FloodWaitError as e:
        return {"ok": False, "error": f"FloodWait {e.seconds}s"}
    finally:
        if imported:
            try:
                await client(DeleteContactsRequest(id=[entity]))
            except Exception:
                pass
    peer = str(entity.id)
    if not store.already_contacted(peer):
        store.mark_contacted(peer, name or phone)
    store.set_peer_phone(peer, phone, name)
    store.log_message(peer, "assistant", text)
    return {"ok": True, "sent_to": phone, "peer_id": entity.id}



WORK_PHONE = os.environ.get("WORK_PHONE", "+380935824369")

OUTREACH_NO_ANSWER = (
    "Доброго дня{name_part}! Мене звати Єва, я помічниця рекрутера компанії "
    "Козир Транс — організація вантажоперевезень.\n\n"
    "Ми телефонували вам щодо вакансії менеджера з продажу логістики, але не "
    "змогли додзвонитися. Умови: повна зайнятість, стовідсотково віддалено, "
    "дохід від тридцяти до шістдесяти п'яти тисяч гривень і вище.\n\n"
    "Якщо вакансія цікава — можемо поспілкуватися тут у чаті, або "
    "зателефонуйте нам на {phone}. Як вам зручніше?"
)

OUTREACH_BAD_CONNECTION = (
    "Доброго дня{name_part}! Мене звати Єва, я помічниця рекрутера компанії "
    "Козир Транс — організація вантажоперевезень.\n\n"
    "Ми щойно спілкувалися телефоном, але зв'язок був поганий і ми не почули "
    "одне одного. Пропоную продовжити тут, у чаті — так буде надійніше.\n\n"
    "Вакансія: менеджер з продажу логістики, повна зайнятість, стовідсотково "
    "віддалено, дохід від тридцяти до шістдесяти п'яти тисяч гривень і вище.\n\n"
    "Підкажіть, будь ласка, вам цікаво?"
)

OUTREACH_COLLECT_INFO = (
    "Доброго дня{name_part}! Це Єва з компанії Козир Транс — ми щойно почали "
    "розмову телефоном щодо вакансії менеджера з продажу логістики, але не "
    "договорили.\n\n"
    "Щоб ми могли розглянути вашу кандидатуру, підкажіть, будь ласка, у якому "
    "місті України ви проживаєте та скільки вам років? Можна відповісти тут "
    "у чаті. Дякую!"
)


OUTREACH_APPLIED_NO_CONTACT = (
    "Доброго дня{name_part}! Мене звати Єва, я помічниця рекрутера компанії "
    "Козир Транс — організація вантажоперевезень.\n\n"
    "Ви відгукувались на нашу вакансію менеджера з продажу логістики, а ми "
    "не встигли з вами зв'язатися. Перепрошуємо.\n\n"
    "Якщо ви ще у пошуку роботи — напишіть, будь ласка, тут у чаті, розкажу "
    "умови й відповім на питання. Або зателефонуйте нам: {phone}."
)

OUTREACH_TEMPLATES = {
    "no_answer": OUTREACH_NO_ANSWER,
    "bad_connection": OUTREACH_BAD_CONNECTION,
    "collect_info": OUTREACH_COLLECT_INFO,
    # The other three all open with "ми телефонували" — they are for people a
    # call already failed to reach. This one is for people we never called at
    # all: the applicants our work.ua intake walked past while it was dead. It
    # promises nothing about the posting still being up, because it is not.
    "applied_no_contact": OUTREACH_APPLIED_NO_CONTACT,
}


def _given_name(full_name: str | None) -> str:
    """Pull the GIVEN name out of a job-board full name.

    Ukrainian boards store "Прізвище Ім'я По-батькові", so taking the first token
    greeted people by surname ("Доброго дня, Вдович!"). With three parts the given
    name is the middle one; with two it is ambiguous, so we greet without a name
    rather than risk the surname again.
    """
    parts = [p for p in (full_name or "").replace(",", " ").split() if p]
    if len(parts) >= 3:
        return parts[1]
    return ""


async def do_send_outreach(phone: str, name: str, kind: str) -> dict:
    """Message someone we failed to reach by phone."""
    tpl = OUTREACH_TEMPLATES.get(kind)
    if not tpl:
        return {"ok": False, "error": f"unknown kind: {kind}"}
    if not STATE.get("active", True):
        return {"ok": False, "error": "Розсилку поставлено на паузу"}
    limit = int(STATE.get("limit", 15))
    if store.sent_today() >= limit:
        return {"ok": False, "error": f"Денний ліміт {limit} вичерпано"}

    phone = phone.strip()
    if not phone:
        return {"ok": False, "error": "no phone"}
    if not phone.startswith("+"):
        phone = "+" + phone

    entity, imported = None, False
    try:
        entity = await client.get_entity(phone)
    except Exception:
        entity = None
    if entity is None:
        try:
            res = await client(ImportContactsRequest(contacts=[
                InputPhoneContact(client_id=0, phone=phone,
                                  first_name=name or "Кандидат", last_name="")]))
            if res.users:
                entity, imported = res.users[0], True
        except Exception as e:
            return {"ok": False, "error": f"import failed: {e}"}
    if entity is None:
        return {"ok": False, "error": "номер не в Telegram або приховує телефон"}

    peer = str(entity.id)
    if store.already_contacted(peer):
        return {"ok": False, "error": "Вже писали цьому кандидату (анти-спам)"}

    first = _given_name(name)
    text = tpl.format(name_part=f", {first}" if first else "", phone=WORK_PHONE)
    try:
        await human_typing(entity, text)
        await client.send_message(entity, text)
    except PeerFloodError:
        STATE["active"] = False
        _save_state(STATE)
        store.log_outreach(phone, kind, False, "peer_flood")
        return {"ok": False, "error": "PeerFloodError — акаунт обмежено"}
    except UserPrivacyRestrictedError:
        store.log_outreach(phone, kind, False, "privacy_restricted")
        return {"ok": False, "error": "приватність: не приймає повідомлення"}
    except FloodWaitError as e:
        store.log_outreach(phone, kind, False, f"flood_wait_{e.seconds}s")
        return {"ok": False, "error": f"FloodWait {e.seconds}s"}
    except Exception as e:
        # PRIVACY_PREMIUM_REQUIRED (403) lands here — Telegram now demands Premium to
        # message some users first. Uncaught, it used to 500 the whole HTTP request.
        code = "privacy_premium_required" if "PREMIUM" in str(e).upper() else f"other: {e}"
        store.log_outreach(phone, kind, False, code)
        return {"ok": False, "error": code}
    finally:
        if imported:
            try:
                await client(DeleteContactsRequest(id=[entity]))
            except Exception:
                pass

    store.log_outreach(phone, kind, True, "")
    store.mark_contacted(peer, name or phone)
    store.set_peer_phone(peer, phone, name)
    store.log_message(peer, "assistant", text)
    return {"ok": True, "kind": kind, "sent_to": phone, "sent_today": store.sent_today()}


async def do_send(target: str, name: str, source: str = "") -> dict:
    if not STATE.get("active", True):
        return {"ok": False, "error": "Розсилку поставлено на паузу"}
    limit = int(STATE.get("limit", 15))
    if store.sent_today() >= limit:
        return {"ok": False, "error": f"Денний ліміт {limit} вичерпано"}
    try:
        entity = await client.get_entity(target)
    except (ValueError, TypeError):
        return {"ok": False, "error": f"Не знайшов {target} (не в Telegram або прихований профіль)"}
    peer = str(entity.id)
    if store.already_contacted(peer):
        return {"ok": False, "error": "Вже писали цьому кандидату (анти-спам)"}
    text = INTRO_TEMPLATE.format(
        vacancy_url=VACANCY_URL,
        name=name or "вітаю",
        source=source or "сайті пошуку роботи",
    )
    try:
        await human_typing(entity, text)
        await client.send_message(entity, text)
    except PeerFloodError:
        STATE["active"] = False
        _save_state(STATE)
        return {"ok": False, "error": "PeerFloodError — Telegram обмежив акаунт. Розсилку зупинено."}
    except UserPrivacyRestrictedError:
        return {"ok": False, "error": "Приватність: юзер не приймає повідомлення від незнайомців"}
    except FloodWaitError as e:
        return {"ok": False, "error": f"FloodWait {e.seconds}s"}
    store.mark_contacted(peer, name or target)
    store.log_message(peer, "assistant", text)
    return {"ok": True, "sent_today": store.sent_today(), "limit": limit}


# ------------------------------ HTTP control ------------------------------
async def h_health(request):
    me = await client.get_me()
    return web.json_response({
        "ok": True,
        "me": f"{me.first_name} @{me.username}",
        "active": STATE.get("active", True),
        "sent_today": store.sent_today(),
        "limit": STATE.get("limit", 15),
    })


async def h_stats(request):
    return web.json_response({
        "sent_today": store.sent_today(),
        "limit": STATE.get("limit", 15),
        "active": STATE.get("active", True),
    })


async def h_send(request):
    body = await request.json()
    res = await do_send(
        str(body.get("target", "")).strip(),
        str(body.get("name", "")).strip(),
        str(body.get("source", "")).strip(),
    )
    return web.json_response(res)


async def h_send_form(request):
    body = await request.json()
    res = await do_send_form(str(body.get("phone", "")).strip(),
                             str(body.get("name", "")).strip())
    return web.json_response(res)


async def h_send_outreach(request):
    body = await request.json()
    res = await do_send_outreach(
        str(body.get("phone", "")).strip(),
        str(body.get("name", "")).strip(),
        str(body.get("kind", "")).strip(),
    )
    return web.json_response(res)


async def h_toggle(request):
    STATE["active"] = not STATE.get("active", True)
    _save_state(STATE)
    return web.json_response({"active": STATE["active"]})


async def h_limit(request):
    body = await request.json()
    try:
        STATE["limit"] = max(1, min(100, int(body.get("value"))))
        _save_state(STATE)
        return web.json_response({"limit": STATE["limit"]})
    except Exception:
        return web.json_response({"error": "bad value"}, status=400)



async def h_resolve(request):
    u = request.query.get("u", "").strip()
    if not u:
        return web.json_response({"ok": False, "error": "no u"}, status=400)
    try:
        entity = await client.get_entity(u)
        name = " ".join(filter(None, [getattr(entity, "first_name", None),
                                      getattr(entity, "last_name", None)]))
        return web.json_response({"ok": True, "id": entity.id, "name": name,
                                  "username": getattr(entity, "username", None)})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=404)


def build_web_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/health", h_health),
        web.get("/stats", h_stats),
        web.get("/resolve", h_resolve),
        web.post("/send", h_send),
        web.post("/send_form", h_send_form),
        web.post("/send_outreach", h_send_outreach),
        web.post("/toggle", h_toggle),
        web.post("/limit", h_limit),
        web.post("/catchup", h_catchup),
        web.get("/outreach_stats", h_outreach_stats),
    ])
    return app



# ------------------------------ chat -> CRM ------------------------------
_CLASSIFY_SYSTEM = """You read a recruiting chat between Eva (assistant) and a
candidate for a B2B logistics-sales manager role at Kozyr Trans. Decide the
outcome so far. Portrait: at least ~1 year real work experience in sales /
logistics / client work (courses or studying do NOT count); lives in ONE of these
oblasts — Житомирська, Хмельницька, Тернопільська, Львівська, Івано-Франківська, Закарпатська, Чернівецька, Рівненська, Волинська, Черкаська, Одеська. Any other oblast does not fit, including Kyiv city AND the whole
Kyiv oblast, and Vinnytsia oblast. Use this list LITERALLY — never reason about
"right bank" or geography, and never invent extra excluded cities.
If the chat mentions the candidate was already rejected before (on a call, or the
candidate says "you told me you don't take people from X"), IGNORE that claim — some
earlier rejections were wrong. Judge the region ONLY against the list above;
age roughly 23-42; ready for full-time with NO side job.

Return ONLY a JSON object:
{"verdict": "qualified" | "not_fit" | "in_progress",
 "reason": "not_target" | "misbehaved" | "not_interested" | "none",
 "region": string|null, "age": integer|null,
 "summary": "1-2 short Ukrainian bullet points of what was learned"}

- "qualified": ONLY if ALL are true — relevant experience (~1y+), region known,
  age known, AND the person is ready for FULL-TIME with no side job (if they
  currently work, they confirmed they will leave it). If any of these is missing or
  they want to combine with another job, it is NOT qualified — "in_progress" or
  "not_fit".
- "not_fit": a real answer shows they do NOT fit (no relevant experience, wrong
  region, side job they will not drop, age outside window).
- "in_progress": not enough answered yet, or still just greetings/questions.
Be conservative: only "qualified"/"not_fit" when the chat truly reached it.

reason (meaningful only when verdict="not_fit"; else "none"):
- "not_target"     — off-portrait from the start: oblast NOT in the allowed list above
                     (excludes the city of Kyiv, and Sumy/Zaporizhzhia/Kherson/Donetsk),
                     age outside ~23-42, or no relevant experience (courses only).
- "misbehaved"     — rude, trolling, insulting, mocking.
- "not_interested" — fits or might fit but declines: found a job, not interested,
                     refuses full-time / wants to combine with another job."""


def _history_to_text(msgs: list[dict]) -> str:
    lines = []
    for m in msgs:
        who = "Кандидат" if m.get("role") == "user" else "Єва"
        lines.append(f"{who}: {m.get('content','')}")
    return "\n".join(lines)


async def classify_dialog(msgs: list[dict]) -> dict | None:
    """Ask Claude for the current outcome of the chat. Returns dict or None."""
    try:
        resp = claude.messages.create(
            model=MODEL, max_tokens=250, system=_CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": _history_to_text(msgs)}],
        )
        await _report_tokens(resp)
        raw = resp.content[0].text.strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return None
        return json.loads(raw[start:end + 1])
    except Exception as e:
        print(f"[classify error] {e}", flush=True)
        return None


async def report_outcome_if_ready(peer: str, sender, msgs: list[dict]) -> None:
    """Classify the chat; on a terminal verdict, push it to the API -> CRM."""
    user_turns = sum(1 for m in msgs if m.get("role") == "user")
    if user_turns < 2:  # not enough answered to judge
        return
    res = await classify_dialog(msgs)
    if not res:
        return
    verdict = res.get("verdict")
    if verdict not in ("qualified", "not_fit"):
        return
    if store.last_outcome(peer) == verdict:  # already reported
        return

    phone = None
    try:
        phone = getattr(sender, "phone", None)
        if phone and not str(phone).startswith("+"):
            phone = "+" + str(phone)
    except Exception:
        phone = None
    if not phone:
        # Hidden number — but if WE reached out to this peer by phone earlier, reuse
        # it so the chat dedupes onto the real candidate/CRM card instead of a
        # synthetic tg<id> one (fixes duplicates + "only a nickname" cards).
        phone = store.get_peer_phone(peer)

    payload = {
        "peer_id": peer,
        "name": " ".join(filter(None, [getattr(sender, "first_name", None),
                                        getattr(sender, "last_name", None)])) or "",
        "username": getattr(sender, "username", None),
        "phone": phone,
        "verdict": verdict,
        "reason": res.get("reason") or "none",
        "region": res.get("region"),
        "age": res.get("age"),
        "summary": res.get("summary") or "",
        "transcript": _history_to_text(msgs)[:6000],
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{API_URL}/internal/tg-outcome",
                json=payload,
                headers={"X-Internal-Token": INTERNAL_API_TOKEN},
                timeout=aiohttp.ClientTimeout(total=25),
            ) as r:
                body = await r.text()
                print(f"[tg->crm] {verdict} peer={peer} -> {r.status} {body[:160]}", flush=True)
                if r.status == 200:
                    store.set_outcome(peer, verdict)
    except Exception as e:
        print(f"[tg->crm error] {e}", flush=True)


# ------------------------------ listener ------------------------------
# NB: this is a helper called from on_message — NOT an event handler. Telethon
# dispatches handlers with a single `event` arg, so registering it here would
# raise TypeError on every incoming message. The @client.on decorator belongs on
# on_message below.
async def _push_progress(peer: str, sender, msgs: list[dict]) -> None:
    """Best-effort: keep an existing CRM card's transcript current with the live chat, so
    a recruiter can read it before a verdict and it survives the candidate deleting the
    chat. No-op server-side for candidates without a card yet."""
    try:
        payload = {
            "peer_id": peer,
            "name": " ".join(filter(None, [getattr(sender, "first_name", None),
                                            getattr(sender, "last_name", None)])) or "",
            "username": getattr(sender, "username", None),
            "phone": store.get_peer_phone(peer),
            "transcript": _history_to_text(msgs)[:6000],
        }
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{API_URL}/internal/tg-progress",
                json=payload,
                headers={"X-Internal-Token": INTERNAL_API_TOKEN},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                await r.text()
    except Exception as e:
        print(f"[tg-progress error] {e}", flush=True)


async def _report_tokens(resp) -> None:
    """Ship this response's Anthropic usage to the API so /costs can see it.

    The userbot has no Postgres — it runs on SQLite in its own container — so the
    numbers go over the same internal endpoint as everything else. Never raises:
    a candidate's reply must not depend on accounting being up.
    """
    try:
        u = getattr(resp, "usage", None)
        if u is None:
            return
        payload = {
            "component": "tg_userbot",
            "model": MODEL,
            "tokens_input": int(getattr(u, "input_tokens", 0) or 0),
            "tokens_output": int(getattr(u, "output_tokens", 0) or 0),
        }
        if not payload["tokens_input"] and not payload["tokens_output"]:
            return
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{API_URL}/internal/token-usage",
                json=payload,
                headers={"X-Internal-Token": INTERNAL_API_TOKEN},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                await r.text()
    except Exception as e:
        print(f"[token-usage error] {e}", flush=True)


async def _should_engage(peer: str, phone: str | None) -> bool:
    """Ask the API whether Eva should still talk to this peer. Once a candidate has
    been handed to a recruiter (manager_review/interview/closed), the API says no and
    Eva stays silent. Fail-open on any error — better to answer than to ghost a live
    candidate over a transient glitch."""
    try:
        params = {"peer": peer}
        if phone:
            params["phone"] = phone
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"{API_URL}/internal/tg-gate",
                params=params,
                headers={"X-Internal-Token": INTERNAL_API_TOKEN},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return True
                data = await r.json()
                return bool(data.get("engage", True))
    except Exception as e:
        print(f"[tg-gate error] {e}", flush=True)
        return True


@client.on(events.NewMessage(incoming=True))
async def on_message(event):
    if not event.is_private:
        return
    if not STATE.get("active", True):
        return
    sender = await event.get_sender()
    if getattr(sender, "bot", False):
        return
    peer = str(sender.id)
    if peer in TG_ADMIN_PEERS:  # a recruiter writing in, not a candidate
        return
    store.log_message(peer, "user", event.raw_text)

    # Debounce: if the candidate fires several messages in a row, wait for the
    # burst to settle and reply ONCE. Each incoming message bumps the peer token;
    # only the handler still holding the latest token proceeds. This kills the
    # duplicate/double replies — previously every message spawned its own Claude
    # reply that overlapped during the human-delay sleep.
    token = _peer_seq.get(peer, 0) + 1
    _peer_seq[peer] = token
    await asyncio.sleep(TG_DEBOUNCE_SEC)
    if _peer_seq.get(peer) != token:
        return  # a newer message arrived — that handler answers the whole burst

    # Once a recruiter owns this candidate (handed off), Eva stays silent. The
    # incoming message is already stored above; we just don't reply.
    if not await _should_engage(peer, store.get_peer_phone(peer)):
        print(f"[tg-gate] silent — recruiter owns peer={peer}", flush=True)
        return

    msgs = store.history(peer)
    try:
        resp = claude.messages.create(model=MODEL, max_tokens=300,
                                      system=SYSTEM_PROMPT, messages=msgs)
        await _report_tokens(resp)
        reply = resp.content[0].text.strip()
    except Exception as e:
        print(f"[claude error] {e}", flush=True)
        return
    # A newer message may have landed while Claude was thinking — drop this stale
    # reply and let the newer handler answer the full context.
    if _peer_seq.get(peer) != token:
        return
    await human_typing(sender, reply)
    await event.respond(reply)
    store.log_message(peer, "assistant", reply)
    await _push_progress(peer, sender, store.history(peer))
    await report_outcome_if_ready(peer, sender, store.history(peer))
    print(f"[{getattr(sender, 'first_name', peer)}] {event.raw_text[:50]!r} -> {reply[:50]!r}", flush=True)


async def catch_up_unread(max_dialogs: int = 20, dry_run: bool = False,
                          only: set[str] | None = None) -> dict:
    """Answer private chats that arrived while Eva was offline.

    on_message only fires on LIVE updates, so anything sent during a restart or an
    outage sits unanswered forever — which is exactly how a misplaced decorator
    silently swallowed three days of candidates. Runs at startup as a safety net and
    can be re-run by hand via POST /catchup (add ?dry=1 to preview the replies
    without sending them).
    """
    if not STATE.get("active", True):
        return {"ok": False, "error": "paused"}
    answered: list[dict] = []
    skipped: list[dict] = []
    try:
        async for dialog in client.iter_dialogs(limit=100):
            if len(answered) >= max_dialogs:
                break
            if not dialog.is_user or dialog.unread_count < 1:
                continue
            sender = dialog.entity
            if getattr(sender, "bot", False):
                continue
            peer = str(sender.id)
            who = getattr(sender, "first_name", None) or peer
            if peer in TG_ADMIN_PEERS:  # colleague, not a candidate
                skipped.append({"peer": peer, "who": who, "why": "admin"})
                continue
            if only is not None and peer not in only:
                skipped.append({"peer": peer, "who": who, "why": "not_selected"})
                continue

            # Store what we missed, oldest first, skipping anything already logged —
            # log_message is a plain INSERT, so the dedupe has to happen here.
            known = {m["content"] for m in store.history(peer, limit=200)
                     if m["role"] == "user"}
            missed: list[str] = []
            async for m in client.iter_messages(sender, limit=min(dialog.unread_count, 20)):
                text = (m.raw_text or "").strip()
                if m.out or not text or text in known:
                    continue
                missed.append(text)
            missed.reverse()
            if not missed:
                skipped.append({"peer": peer, "who": who, "why": "nothing_new"})
                continue
            if not dry_run:
                for text in missed:
                    store.log_message(peer, "user", text)

            if not await _should_engage(peer, store.get_peer_phone(peer)):
                skipped.append({"peer": peer, "who": who, "why": "recruiter_owns"})
                continue

            msgs = store.history(peer)
            if dry_run:  # history has no unsent tail yet — append it for the preview
                msgs = msgs + [{"role": "user", "content": t} for t in missed]
            try:
                resp = claude.messages.create(model=MODEL, max_tokens=300,
                                              system=SYSTEM_PROMPT, messages=msgs)
                await _report_tokens(resp)
                reply = resp.content[0].text.strip()
            except Exception as e:
                print(f"[catchup claude error] {e}", flush=True)
                skipped.append({"peer": peer, "who": who, "why": f"claude: {e}"})
                continue

            if dry_run:
                answered.append({"peer": peer, "who": who, "got": missed, "reply": reply})
                continue
            # One dead peer (deleted account, blocked, flood-wait) must not abort the
            # whole sweep — and nothing counts as answered until it actually went out.
            try:
                await human_typing(sender, reply)
                await client.send_message(sender, reply)
            except Exception as e:
                print(f"[catchup send failed] {who}: {e}", flush=True)
                skipped.append({"peer": peer, "who": who, "why": f"send: {e}"})
                continue
            answered.append({"peer": peer, "who": who, "got": missed, "reply": reply})
            store.log_message(peer, "assistant", reply)
            try:
                await client.send_read_acknowledge(dialog.entity)
                await _push_progress(peer, sender, store.history(peer))
                await report_outcome_if_ready(peer, sender, store.history(peer))
            except Exception as e:  # bookkeeping only — the reply is already delivered
                print(f"[catchup post-send] {who}: {e}", flush=True)
            print(f"[catchup] {who} <- {reply[:60]!r}", flush=True)
            await asyncio.sleep(3)  # space the burst out, the account is rate-limited
    except Exception as e:
        print(f"[catchup error] {e}", flush=True)
        return {"ok": False, "error": str(e), "answered": answered, "skipped": skipped}
    return {"ok": True, "dry_run": dry_run, "answered": answered, "skipped": skipped}


async def h_outreach_stats(request):
    return web.json_response(store.outreach_stats(int(request.query.get("days", 30))))


async def h_catchup(request):
    limit = int(request.query.get("limit", 20))
    dry = request.query.get("dry") in ("1", "true", "yes")
    raw = request.query.get("only", "")
    only = {p.strip() for p in raw.split(",") if p.strip()} or None
    return web.json_response(
        await catch_up_unread(max_dialogs=limit, dry_run=dry, only=only)
    )


async def main():
    await client.start(phone=PHONE)
    me = await client.get_me()
    print(f"Eva online as {me.first_name} @{me.username} ({me.phone})", flush=True)
    runner = web.AppRunner(build_web_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", CTRL_PORT).start()
    print(f"control API on :{CTRL_PORT} | active={STATE.get('active')} limit={STATE.get('limit')}", flush=True)
    # Safety net: whatever landed while we were down never fires on_message, so sweep
    # the unread private chats once we're back up.
    if TG_CATCHUP_ON_START:
        res = await catch_up_unread()
        print(f"[catchup@start] answered={len(res.get('answered') or [])} "
              f"skipped={len(res.get('skipped') or [])}", flush=True)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
