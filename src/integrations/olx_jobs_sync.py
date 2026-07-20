"""OLX Jobs (Ukraine, "Робота на OLX") provider — STUB.

OLX exposes a partner API for high-volume employers. Public scraping is
blocked by Cloudflare; partner API requires written agreement
(`partner-jobs@olx.ua`).

Auth: OAuth2 client_credentials → access_token (1h TTL).
Endpoints:
  - GET /v1/applications?cursor=<cursor>&limit=<n>
  - GET /v1/resumes/{id}
  - GET /v1/listings

Once partner credentials arrive, fill in `OlxJobsClient.list_new_applications`
following the same shape as RobotaUaProvider.
"""
from __future__ import annotations

import structlog

from src.api.inbound_router import InboundRouter
from src.integrations.base import JobBoardProvider, PollResult

log = structlog.get_logger()


class OlxJobsClient:
    BASE_URL = "https://api.olx.ua/jobs/v1"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        # TODO: load from settings.olx_jobs_client_id / settings.olx_jobs_client_secret
        self._client_id = client_id
        self._client_secret = client_secret

    async def list_new_applications(self, cursor: str | None = None) -> list[dict]:
        raise NotImplementedError("OLX Jobs partner API integration pending agreement")


class OlxJobsProvider(JobBoardProvider):
    name = "olx_jobs"
    required_env = ("OLX_JOBS_CLIENT_ID", "OLX_JOBS_CLIENT_SECRET")

    def __init__(self, router: InboundRouter | None = None) -> None:
        self._router = router or InboundRouter()

    async def poll_responses(self) -> PollResult:
        log.info("olx_jobs.poll.skipped", reason="api_not_yet_implemented")
        return PollResult(provider=self.name)

    async def health_check(self) -> bool:
        return False
