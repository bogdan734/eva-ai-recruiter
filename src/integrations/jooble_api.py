"""Jooble Job Search API — STUB.

Jooble is a meta-search aggregator (no candidate responses, only listings).
Useful for proactive sourcing: pull matching listings, find applicants
elsewhere. Not a candidate-response source.

Public API:
  - POST https://jooble.org/api/{api_key}
  - Body: {"keywords": "...", "location": "...", "page": 1}
  - Returns: list of job listings with apply URLs

Intended use: enrich vacancy database, NOT candidate inbound.
"""
from __future__ import annotations

import structlog

from src.common.settings import get_settings

log = structlog.get_logger()


class JoobleClient:
    BASE_URL = "https://jooble.org/api"

    def __init__(self, api_key: str | None = None) -> None:
        s = get_settings()
        self._key = api_key or s.jooble_api_key
        if not self._key:
            raise RuntimeError("JOOBLE_API_KEY not set")

    async def search(self, keywords: str, location: str, page: int = 1) -> dict:
        # TODO: POST {BASE_URL}/{key} with body
        raise NotImplementedError("Jooble integration scaffolded but not active")
