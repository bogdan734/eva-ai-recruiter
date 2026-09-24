"""A dead work.ua session must not be reported as a live one.

The session is alive today. The gap these close is what happens when it is not:
the keepalive refreshes `wsid`'s expiry on every beat, so the cookie file looks
equally healthy whether work.ua still recognises us or has stopped. The report
read that timestamp and would have printed «✅ сесія жива» past the end of the
session, while the keepalive quietly saved the anonymous cookie jar over the
real one — turning an expiry a cookie export would fix into a loss.

So: the keepalive must recognise the anonymous page, refuse to persist it, and
say so; and the report must prefer that verdict over a timestamp that cannot
tell the two apart.

(Worth knowing when testing by hand: work.ua serves the anonymous page to any
browser context without the expected fingerprint, valid cookies or not. Build
it with `_open_context`.)
"""
from __future__ import annotations

import json
import time

import pytest

from src.bot import report as rep
from src.scraper import workua


# --------------------------------------------------------------- the detector

def test_the_anonymous_landing_page_is_recognised():
    """Real markup from work.ua/employer/ while signed out, 18.09.2026."""
    body = (
        "Шукачу Українська Знайти кандидатів Створити вакансію Увійти "
        "Знаходьте співробітників Зараз у нашій базі 4,9 млн кандидатів"
    )
    assert workua._looks_signed_out(body) is True


def test_a_signed_in_page_is_not_mistaken_for_it():
    """The cabinet also links to sign-in flows; the cabinet markers decide."""
    body = "Мій кабінет Мої вакансії Вийти Зареєструватися ще одного користувача"
    assert workua._looks_signed_out(body) is False


def test_an_unrelated_page_is_not_called_signed_out():
    assert workua._looks_signed_out("Резюме Менеджер з продажу Київ") is False


# ------------------------------------------------------------------ the report

@pytest.fixture
def _state(tmp_path, monkeypatch):
    session = tmp_path / "workua_session.json"
    # A cookie that looks perfectly healthy — which is exactly the trap.
    session.write_text(json.dumps({
        "cookies": [{"name": "wsid", "value": "x", "expires": time.time() + 1800}]
    }), encoding="utf-8")

    class _S:
        workua_session_state_path = str(session)
        workua_cold_sourcing_use_api = True

    monkeypatch.setattr("src.common.settings.get_settings", lambda: _S())
    return tmp_path


def test_a_signed_out_session_is_not_reported_as_alive(_state):
    (_state / "workua_session_health.json").write_text(
        json.dumps({"outcome": "logged_out", "at": time.time()}), encoding="utf-8"
    )
    out = rep._workua_session_block()
    assert "сесія жива" not in out, "reported a signed-out session as alive"
    assert "розлогінилась" in out
    assert "cookies" in out, "does not say what the human has to do"


def test_it_says_the_search_still_works_through_the_api(_state):
    """A red session line must not read as "cold sourcing is down" — the API
    path opened 10 contacts on 18.09 with this session signed out."""
    (_state / "workua_session_health.json").write_text(
        json.dumps({"outcome": "logged_out", "at": time.time()}), encoding="utf-8"
    )
    out = rep._workua_session_block()
    assert "через API" in out


def test_a_healthy_session_still_reads_as_healthy(_state):
    (_state / "workua_session_health.json").write_text(
        json.dumps({"outcome": "ok", "at": time.time()}), encoding="utf-8"
    )
    out = rep._workua_session_block()
    assert "сесія жива" in out


def test_no_keepalive_verdict_yet_falls_back_to_the_cookie(_state):
    out = rep._workua_session_block()
    assert "сесія жива" in out
