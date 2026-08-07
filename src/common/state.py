"""Where on-disk poller state lives.

Every long-lived cursor, token and status file the pollers keep belongs in one
directory, and that directory has to be a mounted volume. Code is baked into the
image at build time and `/opt/ai-recruiter` is NOT mounted into the containers,
so anything written to a path inside the image — the historical `.cache/` — is
silently discarded by the next `docker compose build`. For a cursor that means
the poller wakes up believing it has never run and re-ingests its whole backfill
window.

The containers already mount `../state` as `/state` and point `STATE_PATH` at a
file inside it, so that is the directory to reuse. Resolution order:

  1. `ROBOTAUA_STATE_DIR` — explicit override, kept for the robota.ua poller
     that introduced this helper.
  2. the parent of `STATE_PATH` — the mounted volume, how it resolves in prod.
  3. `.cache` — local development, where nothing is containerised.

Lived here since 2026-08-08; previously defined in `robotaua_api` and used only
by the robota.ua pollers, while work.ua kept its own hardcoded `.cache/` path
and lost its cursor on every deploy.
"""
from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    """Directory holding poller cursors, tokens and status files."""
    explicit = (os.getenv("ROBOTAUA_STATE_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    state_path = (os.getenv("STATE_PATH") or "").strip()
    if state_path:
        return Path(state_path).parent
    return Path(".cache")
