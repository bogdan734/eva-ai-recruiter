"""Vapi REST client — initiate outbound calls and fetch call records.

Docs: https://docs.vapi.ai/api-reference/calls/create
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from src.common.settings import get_settings

log = structlog.get_logger()


class VapiClient:
    def __init__(self, token: str | None = None, base_url: str = "https://api.vapi.ai") -> None:
        s = get_settings()
        self._token = token or s.vapi_api_key
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(20.0, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=0.5, max=4))
    async def create_outbound_call(
        self,
        *,
        assistant_id: str,
        phone_number_id: str,
        customer_number_e164: str,
        assistant_overrides: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "assistantId": assistant_id,
            "phoneNumberId": phone_number_id,
            "customer": {"number": customer_number_e164},
        }
        if assistant_overrides:
            body["assistantOverrides"] = assistant_overrides
        if metadata:
            body["metadata"] = metadata
        r = await self._client.post("/call", json=body)
        r.raise_for_status()
        return r.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=0.5, max=4))
    async def get_call(self, call_id: str) -> dict[str, Any]:
        r = await self._client.get(f"/call/{call_id}")
        r.raise_for_status()
        return r.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=0.5, max=4))
    async def list_calls(self, limit: int = 100, **filters: Any) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, **filters}
        r = await self._client.get("/call", params=params)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data
