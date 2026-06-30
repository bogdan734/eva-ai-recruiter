"""Job board provider abstraction.

Every job board integration implements `JobBoardProvider`. The scheduler
calls `poll_responses()` per provider at a configurable cron interval; each
returns normalized `ProviderResponse` items that the InboundRouter ingests
uniformly.

Adding a new board is two files:
  1. `<board>_api.py` — REST/scrape client returning provider-specific dicts
  2. `<board>_sync.py` — implements `JobBoardProvider`, normalizes payloads,
     handles cursor persistence

Register it in `src/integrations/registry.py::ENABLED_PROVIDERS`. The
scheduler picks it up automatically if its required env vars are set.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderResponse:
    """Normalized candidate response from any job board.

    Mirrors the fields the InboundRouter needs to create a KeyCRM lead.
    Provider-specific extras land in `raw` for debugging.
    """
    provider: str
    external_id: str
    full_name: str
    phone_raw: str
    email: str | None = None
    region_raw: str | None = None
    desired_position: str | None = None
    experience_years: int | None = None
    languages: list[str] | None = None
    resume_url: str | None = None
    resume_text: str | None = None
    vacancy_external_id: str | None = None
    received_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PollResult:
    provider: str
    fetched: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    errors: int = 0
    last_cursor: str | None = None


class JobBoardProvider(ABC):
    """Pluggable job board contract.

    Implementations MUST be idempotent: calling `poll_responses()` twice
    with the same upstream state must not double-ingest. Use the cursor
    file under `.cache/<name>_cursor.json` to persist last-processed id.
    """

    name: str = "abstract"
    required_env: tuple[str, ...] = ()

    @abstractmethod
    async def poll_responses(self) -> PollResult:
        """Pull new candidate responses since the last cursor, push each
        through the InboundRouter, advance the cursor."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Best-effort credential + connectivity probe. Used by admin
        Telegram command `/status` to surface broken integrations."""

    @classmethod
    def is_configured(cls, env: dict[str, str]) -> bool:
        """All required env vars are present and non-empty."""
        return all(env.get(k) for k in cls.required_env)
