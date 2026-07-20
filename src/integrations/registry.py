"""Active job board provider registry.

The scheduler imports `enabled_providers()` and registers a `poll_responses`
job for each. Providers are gated on their required env vars — incomplete
config silently skips the provider instead of crashing.

To add a new board:
  1. Implement `<board>_api.py` + `<board>_sync.py` per `base.py`
  2. Add the class here under PROVIDERS
  3. Document its env vars in .env.example + README
"""
from __future__ import annotations

import os

import structlog

from src.integrations.base import JobBoardProvider
from src.integrations.olx_jobs_sync import OlxJobsProvider
from src.integrations.robotaua_sync import RobotaUaProvider

log = structlog.get_logger()


PROVIDERS: list[type[JobBoardProvider]] = [
    RobotaUaProvider,
    OlxJobsProvider,
    # JoobleProvider — sourcing-only, not in inbound poll
]


def enabled_providers() -> list[JobBoardProvider]:
    """Instantiate every provider whose required env vars are set.

    work.ua is NOT in this list — it's already wired directly into the
    scheduler via `workua_sync.poll_responses()` for backwards compat.
    The other providers go through this registry so they share one cadence.
    """
    env = dict(os.environ)
    out: list[JobBoardProvider] = []
    for cls in PROVIDERS:
        if cls.is_configured(env):
            out.append(cls())
            log.info("provider.enabled", name=cls.name)
        else:
            log.debug("provider.skipped", name=cls.name, missing=cls.required_env)
    return out
