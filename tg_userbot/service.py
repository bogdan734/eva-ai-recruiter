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


OUTREACH_TEMPLATES = {
    "no_answer": OUTREACH_NO_ANSWER,
    "bad_connection": OUTREACH_BAD_CONNECTION,
    "collect_info": OUTREACH_COLLECT_INFO,
}


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

    first = (name or "").split()[0] if name else ""
    text = tpl.format(name_part=f", {first}" if first else "", phone=WORK_PHONE)
    try:
        await human_typing(entity, text)
        await client.send_message(entity, text)
    except PeerFloodError:
        STATE["active"] = False
        _save_state(STATE)
        return {"ok": False, "error": "PeerFloodError — акаунт обмежено"}
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

    store.mark_contacted(peer, name or phone)
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
    ])
    return app



# ------------------------------ chat -> CRM ------------------------------
_CLASSIFY_SYSTEM = """You read a recruiting chat between Eva (assistant) and a
candidate for a B2B logistics-sales manager role at Kozyr Trans. Decide the
outcome so far. Portrait: at least ~1 year real work experience in sales /
logistics / client work (courses or studying do NOT count); lives in
right-bank Ukraine (not Kyiv, Sumy, Zaporizhzhia, Kherson, Donetsk obl.);
age roughly 23-42; ready for full-time with NO side job.

Return ONLY a JSON object:
{"verdict": "qualified" | "not_fit" | "in_progress",
 "region": string|null, "age": integer|null,
 "summary": "1-2 short Ukrainian bullet points of what was learned"}

- "qualified": ONLY if the candidate clearly fits AND you know their region AND
  their age AND relevant experience AND they agreed to be passed to a recruiter.
  If region or age is still unknown, it is NOT qualified yet — keep "in_progress".
- "not_fit": a real answer shows they do NOT fit (no relevant experience, wrong
  region, side job they will not drop, age outside window).
- "in_progress": not enough answered yet, or still just greetings/questions.
Be conservative: only "qualified"/"not_fit" when the chat truly reached it."""


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

    payload = {
        "peer_id": peer,
        "name": " ".join(filter(None, [getattr(sender, "first_name", None),
                                        getattr(sender, "last_name", None)])) or "",
        "username": getattr(sender, "username", None),
        "phone": phone,
        "verdict": verdict,
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
    store.log_message(peer, "user", event.raw_text)
    msgs = store.history(peer)
    try:
        resp = claude.messages.create(model=MODEL, max_tokens=300,
                                      system=SYSTEM_PROMPT, messages=msgs)
        reply = resp.content[0].text.strip()
    except Exception as e:
        print(f"[claude error] {e}", flush=True)
        return
    await asyncio.sleep(random.uniform(4, 20))
    await human_typing(sender, reply)
    await event.respond(reply)
    store.log_message(peer, "assistant", reply)
    await report_outcome_if_ready(peer, sender, store.history(peer))
    print(f"[{getattr(sender, 'first_name', peer)}] {event.raw_text[:50]!r} -> {reply[:50]!r}", flush=True)


async def main():
    await client.start(phone=PHONE)
    me = await client.get_me()
    print(f"Eva online as {me.first_name} @{me.username} ({me.phone})", flush=True)
    runner = web.AppRunner(build_web_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", CTRL_PORT).start()
    print(f"control API on :{CTRL_PORT} | active={STATE.get('active')} limit={STATE.get('limit')}", flush=True)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
