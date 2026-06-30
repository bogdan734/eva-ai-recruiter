"""robota.ua REST client — STUB.

Status: scaffolding only. robota.ua public API requires partner agreement
(`partner-api@robota.ua`). Once granted:

  - Auth: Bearer token in `Authorization` header
  - Base URL: https://api.robota.ua/v2
  - Endpoints we need:
      GET /vacancies/{id}/applications      → list new applications
      GET /resumes/{id}                     → full resume payload
      GET /vacancies                        → our active vacancies
  - Rate limit: 60 req/min per token (last published spec, 2025-Q4)

Fill in the TODOs once credentials and the latest OpenAPI spec land.
"""
from __future__ import annotations

import structlog

from src.common.settings import get_settings

log = structlog.get_logger()


class RobotaUaApiError(RuntimeError):
    pass


class RobotaUaAuthError(RobotaUaApiError):
    pass


class RobotaUaClient:
    BASE_URL = "https://api.robota.ua/v2"

    def __init__(self, token: str | None = None) -> None:
        s = get_settings()
        self._token = token or s.robotaua_api_token
        if not self._token:
            raise RobotaUaAuthError("ROBOTAUA_API_TOKEN not set")

    async def list_new_applications(self, since_id: int | None = None) -> list[dict]:
        # TODO: GET /vacancies/applications?since_id=...
        raise NotImplementedError("robota.ua API integration pending partner approval")

    async def get_resume(self, resume_id: int) -> dict:
        # TODO: GET /resumes/{resume_id}
        raise NotImplementedError("robota.ua API integration pending partner approval")

    async def list_vacancies(self) -> list[dict]:
        # TODO: GET /vacancies?employer_id=<self>
        raise NotImplementedError("robota.ua API integration pending partner approval")


def parse_application(payload: dict) -> dict:
    """robota.ua application dict → IngestPayload-friendly fields.

    TODO: implement once the actual JSON shape is known. Expected keys
    (extrapolated from public partner docs):
      - id, candidate.name, candidate.phone, candidate.email
      - candidate.city, vacancy.id, applied_at, resume.url
    """
    raise NotImplementedError
