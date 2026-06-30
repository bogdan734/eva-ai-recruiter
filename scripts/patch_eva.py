"""Patch Vapi Єва assistant: switch to stable model + render inbound-safe prompt + (re-)attach phoneNumber server secret."""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, "/app")

from src.call.script_template import render_system_prompt  # noqa: E402

VAPI_API = "https://api.vapi.ai"


async def main() -> None:
    api_key = os.environ["VAPI_API_KEY"]
    assistant_id = os.environ["VAPI_ASSISTANT_ID"]
    phone_id = os.environ["VAPI_PHONE_NUMBER_ID"]
    webhook_secret = os.environ["VAPI_WEBHOOK_SECRET"]

    inbound_prompt = render_system_prompt(
        candidate_name="невідомий (вхідний дзвінок)",
        candidate_phone="невідомий",
        candidate_position="невідомо (запитати у кандидата)",
        candidate_region="невідомо (запитати у кандидата)",
        source="inbound_call",
    )

    assistant_patch = {
        "model": {
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "maxTokens": 200,
            "temperature": 0.4,
            "messages": [{"role": "system", "content": inbound_prompt}],
        },
        "firstMessage": "Доброго дня!",
        "silenceTimeoutSeconds": 30,
        "responseDelaySeconds": 0.1,
        "llmRequestDelaySeconds": 0.02,
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-2",
            "language": "uk",
            "endpointing": 150,
            "smartFormat": True,
        },
    }

    phone_patch = {
        "assistantId": assistant_id,
        "server": {
            "url": "https://api.kozyrtrans-ai.com/webhooks/vapi/events",
            "secret": webhook_secret,
        },
    }

    async with httpx.AsyncClient(
        base_url=VAPI_API,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    ) as client:
        r1 = await client.patch(f"/assistant/{assistant_id}", json=assistant_patch)
        r1.raise_for_status()
        a = r1.json()
        print(f"assistant.model.model: {a['model']['model']}")
        print(f"assistant.silenceTimeoutSeconds: {a.get('silenceTimeoutSeconds')}")
        sys_msg = a["model"]["messages"][0]["content"]
        print(f"assistant.system_prompt length: {len(sys_msg)} chars")
        print(f"placeholders left: {'{CANDIDATE_' in sys_msg}")

        r2 = await client.patch(f"/phone-number/{phone_id}", json=phone_patch)
        r2.raise_for_status()
        p = r2.json()
        print(f"phone.assistantId: {p.get('assistantId')}")
        print(f"phone.server.url: {p.get('server', {}).get('url')}")
        print(f"phone.isServerUrlSecretSet: {p.get('isServerUrlSecretSet')}")


if __name__ == "__main__":
    asyncio.run(main())
