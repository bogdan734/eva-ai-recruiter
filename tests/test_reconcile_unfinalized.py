"""reconcile_unfinalized is the safety net for calls whose end-of-call webhook never arrived.

It used to reference an undefined `s` for the Vapi base URL, so on every run that found a
stuck call it died with NameError before asking Vapi anything — the safety net was off.
"""
from contextlib import asynccontextmanager

import httpx
import pytest

import src.call.orchestrator as orchestrator
import src.scheduler.dispatcher as dispatcher
from src.common.settings import get_settings


class _Rows:
    def scalars(self):
        return self

    def all(self):
        return ["call-1"]


class _Session:
    async def execute(self, _query):
        return _Rows()


@asynccontextmanager
async def _session_scope():
    yield _Session()


class _Client:
    opened_with: dict = {}

    def __init__(self, **kwargs):
        _Client.opened_with = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path):
        return httpx.Response(404, request=httpx.Request("GET", "https://vapi.test" + path))


@pytest.mark.asyncio
async def test_reconcile_asks_vapi_at_the_configured_base_url(monkeypatch):
    monkeypatch.setenv("VAPI_API_KEY", "test-key")
    monkeypatch.setattr(dispatcher, "session_scope", _session_scope)
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(orchestrator, "CallOrchestrator", lambda: object())

    await dispatcher.reconcile_unfinalized()

    assert _Client.opened_with["base_url"] == get_settings().vapi_base_url
