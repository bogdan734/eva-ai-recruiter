"""KeyCRM Open API v1 client — Kozyr Trans setup.

Live structure discovered 2026-06-23:
- Funnel (pipeline) id=1 "1 Етап Менеджер з продажу"
- Statuses: 1=Новий, 2=Відібрано, 4=Дійшов на 1 тур,
            32=Не актуально, 33=Не підходить нам, 34=Не ЦА
- Default manager: id=3 Svitlana Kozyrtrans
- Existing custom fields: LD_1001 Вакансія, LD_1002 Номер вакансії,
  LD_1003 Опис вакансії, LD_1004 Посилання на вакансію
- DELETE on /pipelines/cards/<id> not allowed → move to status 32 instead
- Custom fields creation NOT exposed via API — must be created in UI
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from src.common.settings import get_settings

log = structlog.get_logger()

# Live values from current Kozyr Trans KeyCRM
FUNNEL_ID = 1

STATUS_NEW = 1                  # default for newly created — "Новий"
STATUS_QUALIFIED = 2            # AI confirmed → manager review — "Відібрано"
STATUS_INTERVIEW_PASSED = 4     # interview happened — "Дійшов на 1 тур"
STATUS_NOT_INTERESTED = 32      # final — "Не актуально"
STATUS_WE_REJECTED = 33         # final — "Не підходить нам"
STATUS_BLACKLIST = 34           # final — "Не ЦА"
DEFAULT_MANAGER_ID = 3          # Svitlana Kozyrtrans

# Custom-field UUIDs (created in KeyCRM UI)
# Original 4 (manual entry by HR)
FIELD_VACANCY = "LD_1001"       # select: vacancy name
FIELD_RESPONSE_ID = "LD_1002"   # text: work.ua response id
FIELD_RESUME_TEXT = "LD_1003"   # textarea: full resume
FIELD_RESUME_URL = "LD_1004"    # link: work.ua resume URL
# AI-recruiter fields (added 2026-06-23 via Chrome MCP automation)
FIELD_AI_AUDIO = "LD_1005"      # link: recording URL
FIELD_AI_TRANSCRIPT = "LD_1006" # text: full call transcript
FIELD_AI_SUMMARY = "LD_1007"    # text: 3-bullet AI summary
FIELD_AI_MATCH_SCORE = "LD_1008" # integer: 0-100
FIELD_AI_REGION = "LD_1009"     # text: normalized region

# New fields created in the KeyCRM UI (Вік / Місто / Посилання на резюме). Their
# UUIDs are assigned by KeyCRM, so we resolve them by display name at runtime.
_EXTRA_FIELD_NAMES = {
    "age": "Вік",
    "city": "Місто",
    "resume_link": "Посилання на резюме",
}

# Prefix of the one line Eva keeps on the contact (buyer) note so recruiters see
# call status at the contact level. The recruiter's own manual note is preserved.
_AI_NOTE_PREFIX = "🤖 Єва"

# Marker separating the recruiter/status head of a contact note from the Telegram
# dialog block that Eva keeps refreshed underneath it.
_TG_DIALOG_MARKER = "──── Telegram ────"


def _is_real_phone(phone: str | None) -> bool:
    """True for an international (+…) number. Telegram surrogates ('tg<peer>') and
    blanks are not real phones — we never store them on a buyer or dedupe by them."""
    return bool(phone) and phone.strip().startswith("+")


class KeyCRMClient:
    """Thin async wrapper over KeyCRM Open API v1."""

    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        s = get_settings()
        self._token = token or s.keycrm_api_token
        self._base = (base_url or s.keycrm_base_url).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_rate_limited(self, url: str, **kw) -> httpx.Response:
        """GET that survives KeyCRM's ~60 req/min ceiling.

        Without this a 429 propagated as an exception, and callers that treat a
        failed lookup as "nothing found" would act on a wrong answer. Waits out
        Retry-After rather than hammering.
        """
        last: httpx.Response | None = None
        for attempt in range(4):
            r = await self._client.get(url, **kw)
            if r.status_code != 429:
                return r
            last = r
            try:
                pause = float(r.headers.get("Retry-After") or 15)
            except ValueError:
                pause = 15.0
            pause = min(pause, 60) * (attempt + 1)
            log.warning("keycrm.rate_limited", url=url, wait=pause, attempt=attempt + 1)
            await asyncio.sleep(pause)
        return last if last is not None else await self._client.get(url, **kw)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=0.5, max=4))
    async def find_lead_by_phone(
        self, phone_e164: str, *, pipeline_id: int | None = None
    ) -> dict[str, Any] | None:
        """Search leads by contact phone. KeyCRM uses /pipelines/cards with filter.

        ⚠️ THIS DOES NOT WORK AND NEVER HAS. KeyCRM rejects the filter outright:

            HTTP 400 — Requested filter(s) `contact.phone` are not allowed.
            Allowed filter(s) are `pipeline_id, status_id, source_id,
            created_between, updated_between`.

        Verified 2026-08-05. Every call raises, so any caller that treats a failed
        lookup as "no duplicate found" will happily create duplicates forever —
        which is exactly what happened to the accountant funnel that day.

        Dedup by phone therefore has to be LOCAL, against our own `candidates`
        table (see InboundRouter). Kept here only so the failure is documented in
        one place rather than rediscovered; do not build on it.

        `pipeline_id` narrows the answer to one funnel — relevant only if this is
        ever reimplemented on an endpoint that supports phone lookup, e.g. via
        `find_buyer_by_phone`, which uses /buyer and does work.
        """
        r = await self._get_rate_limited(
            "/pipelines/cards",
            params={
                "filter[contact.phone]": phone_e164,
                "limit": 1 if pipeline_id is None else 50,
                "include": "contact",
            },
        )
        r.raise_for_status()
        data = r.json().get("data") or []
        if pipeline_id is None:
            return data[0] if data else None
        for card in data:
            if card.get("pipeline_id") == pipeline_id:
                return card
        return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=0.5, max=4))
    async def find_buyer_by_phone(self, phone: str) -> int | None:
        """KeyCRM auto-dedupes buyers (contacts) by phone — return the existing
        buyer id for this phone, or None. (Cards can't be filtered by phone via the
        API, but buyers can — this is how we reach the shared contact.)"""
        r = await self._client.get("/buyer", params={"filter[buyer_phone]": phone, "limit": 1})
        r.raise_for_status()
        data = (r.json() or {}).get("data") or []
        return int(data[0]["id"]) if data and data[0].get("id") else None

    async def write_buyer_call_status(self, buyer_id: int, status_line: str) -> None:
        """Keep ONE Eva status line on the contact note (prepended), so a recruiter
        sees 'called? / result' on the contact without opening each card. The
        recruiter's own manual note lines are preserved; only our previous Eva line
        is replaced."""
        try:
            r = await self._client.get(f"/buyer/{buyer_id}")
            r.raise_for_status()
            note = (r.json() or {}).get("note") or ""
            kept = "\n".join(ln for ln in note.split("\n") if not ln.startswith(_AI_NOTE_PREFIX)).strip()
            new_note = (f"{_AI_NOTE_PREFIX} · {status_line}" + (("\n" + kept) if kept else ""))[:2000]
            await self._client.put(f"/buyer/{buyer_id}", json={"note": new_note})
        except Exception as e:
            log.warning("keycrm.buyer_note_failed", buyer_id=buyer_id, error=str(e))

    async def write_buyer_dialog(self, buyer_id: int, dialog_text: str) -> None:
        """Keep the full Telegram conversation on the contact (buyer) note, so a recruiter
        reads it 1-to-1 even before a verdict and even if the candidate deletes the chat.
        The Eva status line and the recruiter's own notes above the marker are preserved;
        only the dialog block is refreshed."""
        try:
            r = await self._client.get(f"/buyer/{buyer_id}")
            r.raise_for_status()
            note = (r.json() or {}).get("note") or ""
            head = note.split(_TG_DIALOG_MARKER, 1)[0].rstrip()
            block = _TG_DIALOG_MARKER + "\n" + (dialog_text or "").strip()
            new_note = ((head + "\n" + block) if head else block)[:2000]
            await self._client.put(f"/buyer/{buyer_id}", json={"note": new_note})
        except Exception as e:
            log.warning("keycrm.buyer_dialog_failed", buyer_id=buyer_id, error=str(e))

    @retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=0.5, max=4))
    async def create_buyer(
        self,
        *,
        full_name: str,
        phone: str | None = None,
        email: str | None = None,
        manager_id: int = DEFAULT_MANAGER_ID,
        note: str | None = None,
    ) -> int | None:
        """Create a saved buyer (client). This is what earns the green 'client'
        check on a card. Only a real (+…) phone is stored — a Telegram surrogate
        becomes a name-only buyer (client wants every contact saved, number or not)."""
        body: dict[str, Any] = {"full_name": (full_name or "Без імені")[:255]}
        if _is_real_phone(phone):
            body["phone"] = [phone]
        if email:
            body["email"] = [email]
        if manager_id:
            body["manager_id"] = manager_id
        if note:
            body["note"] = note[:2000]
        r = await self._client.post("/buyer", json=body)
        r.raise_for_status()
        return int((r.json() or {}).get("id") or 0) or None

    async def ensure_buyer(
        self,
        *,
        full_name: str,
        phone: str | None = None,
        email: str | None = None,
        manager_id: int = DEFAULT_MANAGER_ID,
    ) -> int | None:
        """Return a saved-buyer id for this person, creating one if none exists, so
        a card can be linked to it (green check). Dedupe by phone when it's real —
        KeyCRM keeps one buyer per number. Returns None only on hard API failure."""
        try:
            if _is_real_phone(phone):
                existing = await self.find_buyer_by_phone(phone)
                if existing:
                    return existing
            return await self.create_buyer(
                full_name=full_name, phone=phone, email=email, manager_id=manager_id
            )
        except Exception as e:
            log.warning("keycrm.ensure_buyer_failed", error=str(e), phone=phone)
            return None

    async def get_card_status(self, lead_id: int) -> int | None:
        """Current KeyCRM status_id (stage) of a card, or None if it's gone/errored.
        Used to detect recruiters' manual stage moves and stop Eva accordingly."""
        try:
            r = await self._client.get(f"/pipelines/cards/{lead_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            sid = (r.json() or {}).get("status_id")
            return int(sid) if sid is not None else None
        except Exception as e:
            log.warning("keycrm.get_card_status_failed", error=str(e), lead_id=lead_id)
            return None

    async def card_pipeline(self, lead_id: int) -> int | None:
        """Which funnel this card is in, or None if it no longer exists.

        Recruiters delete cards from the KeyCRM UI, and our stored
        `keycrm_lead_id` then points at nothing. Treating that stale id as "this
        person already has a card" makes the candidate permanently invisible —
        21 accountants were lost that way on 2026-08-05.
        """
        try:
            r = await self._get_rate_limited(f"/pipelines/cards/{lead_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            pid = (r.json() or {}).get("pipeline_id")
            return int(pid) if pid is not None else None
        except Exception as e:  # noqa: BLE001
            log.warning("keycrm.card_lookup_failed", lead_id=lead_id, error=str(e))
            raise  # caller must fail closed, not assume the card is gone

    async def link_card_to_buyer(self, card_id: int, buyer_id: int) -> None:
        """Attach an existing saved buyer to a card — sets the green 'client' check.
        KeyCRM applies this asynchronously (PUT returns 202, effect lands seconds
        later). Idempotent: re-linking the same buyer is a no-op."""
        try:
            await self._client.put(
                f"/pipelines/cards/{card_id}", json={"client_id": buyer_id}
            )
        except Exception as e:
            log.warning(
                "keycrm.link_card_failed", error=str(e), card_id=card_id, buyer_id=buyer_id
            )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=0.5, max=4))
    async def create_lead(
        self,
        *,
        title: str,
        full_name: str,
        phone: str,
        email: str | None = None,
        vacancy_name: str = "Менеджер з продажу",
        workua_response_id: str | None = None,
        resume_text: str | None = None,
        resume_url: str | None = None,
        manager_comment: str | None = None,
        ai_audio_url: str | None = None,
        ai_transcript: str | None = None,
        ai_summary: str | None = None,
        ai_match_score: int | None = None,
        ai_region: str | None = None,
        pipeline_id: int = FUNNEL_ID,
        status_id: int = STATUS_NEW,
        source_id: int = 1,
        manager_id: int = DEFAULT_MANAGER_ID,
        save_buyer: bool = True,
    ) -> dict[str, Any]:
        """Create a lead with contact + custom fields in one call."""
        custom: list[dict[str, Any]] = []
        if vacancy_name:
            custom.append({"uuid": FIELD_VACANCY, "value": [vacancy_name]})
        if workua_response_id:
            custom.append({"uuid": FIELD_RESPONSE_ID, "value": str(workua_response_id)})
        if resume_text:
            custom.append({"uuid": FIELD_RESUME_TEXT, "value": resume_text[:8000]})
        if resume_url:
            custom.append({"uuid": FIELD_RESUME_URL, "value": resume_url})
        if ai_audio_url:
            custom.append({"uuid": FIELD_AI_AUDIO, "value": ai_audio_url})
        if ai_transcript:
            custom.append({"uuid": FIELD_AI_TRANSCRIPT, "value": ai_transcript[:8000]})
        if ai_summary:
            custom.append({"uuid": FIELD_AI_SUMMARY, "value": ai_summary[:2000]})
        if ai_match_score is not None:
            custom.append({"uuid": FIELD_AI_MATCH_SCORE, "value": int(ai_match_score)})
        if ai_region:
            custom.append({"uuid": FIELD_AI_REGION, "value": ai_region})

        # Save the person as a buyer (client) first, then create the card ON that
        # buyer via contact.client_id — that is what shows the green 'client' check.
        # Fall back to an inline contact only if the buyer step fails hard.
        #
        # save_buyer=False leaves the contact inline and UNSAVED, which is how the
        # sales funnel's cards look: «Контактні дані» with a «Зберегти покупця»
        # button instead of a green «Покупець». Recruiters decide who becomes a
        # client in the contact book; an intake card should not pre-empt that.
        buyer_id = None
        if save_buyer:
            buyer_id = await self.ensure_buyer(
                full_name=full_name, phone=phone, email=email, manager_id=manager_id
            )
        body: dict[str, Any] = {
            "title": title,
            "pipeline_id": pipeline_id,
            "status_id": status_id,
            "source_id": source_id,
            "manager_id": manager_id,
        }
        if buyer_id:
            body["contact"] = {"client_id": buyer_id}
        else:
            body["contact"] = {"full_name": full_name, "phone": phone}
            if email:
                body["contact"]["email"] = email
        if manager_comment:
            body["manager_comment"] = manager_comment
        if custom:
            body["custom_fields"] = custom

        r = await self._client.post("/pipelines/cards", json=body)
        r.raise_for_status()
        return r.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=0.5, max=4))
    async def update_lead(self, lead_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.put(f"/pipelines/cards/{lead_id}", json=payload)
        r.raise_for_status()
        return r.json() if r.text else {}

    async def move_to_status(self, lead_id: int, status_id: int) -> dict[str, Any]:
        return await self.update_lead(lead_id, {"status_id": status_id})

    async def write_call_results(
        self,
        lead_id: int,
        *,
        summary: str | None = None,
        transcript: str | None = None,
        audio_url: str | None = None,
        region: str | None = None,
        match_score: int | None = None,
        age: int | None = None,
        city: str | None = None,
        resume_link: str | None = None,
    ) -> dict[str, Any]:
        """Push what Eva learned on the call into the card's AI fields.

        Also keeps the vacancy fields (LD_1001/1002/1004) populated on every card
        — the client wants these auto-filled, not entered by hand — and fills the
        new Вік / Місто / Посилання на резюме fields when they exist in KeyCRM.
        """
        s = get_settings()
        custom: list[dict[str, Any]] = []
        # Vacancy fields — always present so no card is left blank.
        custom.append({"uuid": FIELD_VACANCY, "value": [s.keycrm_vacancy_label or "Менеджер з продажу"]})
        if getattr(s, "vacancy_number", ""):
            custom.append({"uuid": FIELD_RESPONSE_ID, "value": str(s.vacancy_number)})
        if getattr(s, "vacancy_url", ""):
            custom.append({"uuid": FIELD_RESUME_URL, "value": s.vacancy_url})
        if summary:
            custom.append({"uuid": FIELD_AI_SUMMARY, "value": summary[:8000]})
        if transcript:
            custom.append({"uuid": FIELD_AI_TRANSCRIPT, "value": transcript[:8000]})
        if audio_url:
            custom.append({"uuid": FIELD_AI_AUDIO, "value": audio_url})
        if region:
            custom.append({"uuid": FIELD_AI_REGION, "value": region[:250]})
        if match_score is not None:
            custom.append({"uuid": FIELD_AI_MATCH_SCORE, "value": int(match_score)})
        # New fields resolved by name — skipped silently until created in the UI.
        extra = await self._resolve_extra_fields()
        if age and extra.get("age"):
            custom.append({"uuid": extra["age"], "value": str(age)})
        if city and extra.get("city"):
            custom.append({"uuid": extra["city"], "value": city[:250]})
        if resume_link and extra.get("resume_link"):
            custom.append({"uuid": extra["resume_link"], "value": resume_link})
        if not custom:
            return {}
        return await self.update_lead(lead_id, {"custom_fields": custom})

    _extra_cache: dict[str, str] | None = None

    async def _resolve_extra_fields(self) -> dict[str, str]:
        """Map tech_key -> KeyCRM uuid for the new Вік/Місто/Посилання на резюме
        fields by their display name. Cached once all three are found; until then
        we re-query so a field created later is picked up without a restart."""
        if KeyCRMClient._extra_cache is not None:
            return KeyCRMClient._extra_cache
        cache: dict[str, str] = {}
        try:
            r = await self._client.get("/custom-fields")
            for f in (r.json() or []):
                if f.get("model") != "lead":
                    continue
                for key, nm in _EXTRA_FIELD_NAMES.items():
                    if f.get("name") == nm and f.get("uuid"):
                        cache[key] = f["uuid"]
        except Exception:
            pass
        if len(cache) == len(_EXTRA_FIELD_NAMES):
            KeyCRMClient._extra_cache = cache
        return cache

    async def assign_manager(self, lead_id: int, manager_id: int) -> dict[str, Any]:
        """Set the responsible user — used to flag AI-handled leads."""
        return await self.update_lead(lead_id, {"manager_id": manager_id})

    async def append_manager_comment(self, lead_id: int, addition: str) -> dict[str, Any]:
        r = await self._client.get(f"/pipelines/cards/{lead_id}")
        r.raise_for_status()
        existing = (r.json() or {}).get("manager_comment") or ""
        merged = f"{existing}\n\n--- AI {addition}".strip() if existing else f"AI {addition}"
        return await self.update_lead(lead_id, {"manager_comment": merged[:5000]})

    async def get_lead(self, lead_id: int, include: str = "contact,customFields,status") -> dict[str, Any]:
        r = await self._client.get(f"/pipelines/cards/{lead_id}", params={"include": include})
        r.raise_for_status()
        return r.json()
