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

from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from src.common.settings import get_settings

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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=0.5, max=4))
    async def find_lead_by_phone(self, phone_e164: str) -> dict[str, Any] | None:
        """Search leads by contact phone. KeyCRM uses /pipelines/cards with filter."""
        r = await self._client.get(
            "/pipelines/cards",
            params={"filter[contact.phone]": phone_e164, "limit": 1, "include": "contact"},
        )
        r.raise_for_status()
        data = r.json().get("data") or []
        return data[0] if data else None

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

        body: dict[str, Any] = {
            "title": title,
            "pipeline_id": pipeline_id,
            "status_id": status_id,
            "source_id": source_id,
            "manager_id": manager_id,
            "contact": {"full_name": full_name, "phone": phone},
        }
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
