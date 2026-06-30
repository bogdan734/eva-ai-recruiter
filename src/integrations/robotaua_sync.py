"""robota.ua → KeyCRM sync — STUB.

Mirrors the work.ua sync flow:
  1. poll new applications since cursor
  2. normalize each to ProviderResponse
  3. push through InboundRouter
  4. advance cursor

Cursor file: `.cache/robotaua_cursor.json` (gitignored).

Wire-up: once `ROBOTAUA_API_TOKEN` is set in `.env`, the scheduler will
register this provider's `poll_responses` job at the same 5-min cadence
as work.ua.
"""
from __future__ import annotations

import json
from pathlib import Path

import structlog

from src.api.inbound_router import IngestPayload, InboundRouter
from src.integrations.base import JobBoardProvider, PollResult, ProviderResponse
from src.integrations.robotaua_api import RobotaUaClient, parse_application

log = structlog.get_logger()

CURSOR_PATH = Path(".cache/robotaua_cursor.json")


class RobotaUaProvider(JobBoardProvider):
    name = "robotaua"
    required_env = ("ROBOTAUA_API_TOKEN",)

    def __init__(
        self,
        client: RobotaUaClient | None = None,
        router: InboundRouter | None = None,
    ) -> None:
        self._client = client
        self._router = router or InboundRouter()

    def _load_cursor(self) -> int | None:
        if not CURSOR_PATH.exists():
            return None
        try:
            return int(json.loads(CURSOR_PATH.read_text()).get("last_id", 0)) or None
        except Exception:
            return None

    def _save_cursor(self, last_id: int) -> None:
        CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        CURSOR_PATH.write_text(json.dumps({"last_id": last_id}))

    async def poll_responses(self) -> PollResult:
        result = PollResult(provider=self.name)
        client = self._client or RobotaUaClient()
        try:
            applications = await client.list_new_applications(since_id=self._load_cursor())
        except NotImplementedError:
            log.info("robotaua.poll.skipped", reason="api_not_yet_implemented")
            return result
        except Exception as e:
            log.error("robotaua.poll.failed", error=str(e))
            result.errors += 1
            return result

        for app in applications:
            result.fetched += 1
            try:
                fields = parse_application(app)
                pr = ProviderResponse(
                    provider=self.name,
                    external_id=str(fields["external_id"]),
                    full_name=fields["full_name"],
                    phone_raw=fields["phone_raw"],
                    email=fields.get("email"),
                    region_raw=fields.get("region_raw"),
                    desired_position=fields.get("desired_position"),
                    vacancy_external_id=fields.get("vacancy_external_id"),
                    raw=app,
                )
                ingest = await self._router.ingest(
                    IngestPayload(
                        full_name=pr.full_name,
                        phone_raw=pr.phone_raw,
                        email=pr.email,
                        region_raw=pr.region_raw,
                        desired_position=pr.desired_position,
                        source=self.name,
                    )
                )
                if ingest.duplicate:
                    result.duplicates += 1
                elif ingest.accepted:
                    result.accepted += 1
                else:
                    result.rejected += 1
                result.last_cursor = str(pr.external_id)
            except Exception as e:
                result.errors += 1
                log.warning("robotaua.ingest.failed", error=str(e))

        if result.last_cursor:
            try:
                self._save_cursor(int(result.last_cursor))
            except Exception:
                pass
        return result

    async def health_check(self) -> bool:
        try:
            client = self._client or RobotaUaClient()
            await client.list_vacancies()
            return True
        except NotImplementedError:
            return False
        except Exception:
            return False
