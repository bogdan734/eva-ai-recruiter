"""The hang this guards against: a bare `await proc.wait()` after kill() that
never returns, which the poller kept mistaking for a hung curl."""
import asyncio
import time

import pytest

from src.integrations.robotaua_api import _REAP_TIMEOUT, _reap


@pytest.mark.asyncio
async def test_reap_kills_a_running_process():
    proc = await asyncio.create_subprocess_exec("sleep", "30")
    started = time.monotonic()
    await _reap(proc, "https://example.test/x")
    assert proc.returncode is not None
    assert time.monotonic() - started < _REAP_TIMEOUT


@pytest.mark.asyncio
async def test_reap_escalates_past_a_process_that_ignores_sigterm():
    """SIGTERM is swallowed, so only the SIGKILL step can collect it."""
    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", 'trap "" TERM; sleep 30'
    )
    started = time.monotonic()
    await _reap(proc, "https://example.test/x")
    elapsed = time.monotonic() - started
    assert proc.returncode is not None
    # One full terminate window, then the kill lands — never the unbounded wait.
    assert elapsed < _REAP_TIMEOUT * 2 + 2


@pytest.mark.asyncio
async def test_reap_is_a_noop_on_an_already_dead_process():
    proc = await asyncio.create_subprocess_exec("true")
    await proc.wait()
    rc = proc.returncode
    await _reap(proc, "https://example.test/x")  # must not raise
    assert proc.returncode == rc
