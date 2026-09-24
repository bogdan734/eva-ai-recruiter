"""The report must say when cold sourcing has no usable session.

A silent no-op reads exactly like "nobody matched today" -- which is how the
work.ua session dying went unnoticed on 09.09.2026.
"""
import json
import time

import pytest

from src.bot import report


def _write(tmp_path, cookies):
    p = tmp_path / "workua_session.json"
    p.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    return p


@pytest.fixture
def _settings(monkeypatch, tmp_path):
    class _S:
        workua_session_state_path = str(tmp_path / "workua_session.json")

    monkeypatch.setattr("src.common.settings.get_settings", lambda: _S())
    return _S


def test_missing_file_is_called_out(_settings):
    out = report._workua_session_block()
    assert "немає збереженої сесії" in out


def test_expired_session_asks_for_a_new_export(_settings, tmp_path):
    _write(tmp_path, [{"name": "wsid", "expires": time.time() - 60}])
    out = report._workua_session_block()
    assert "протухла" in out
    assert "Cookie-Editor" in out


def test_live_session_reports_remaining_minutes(_settings, tmp_path):
    _write(tmp_path, [{"name": "wsid", "expires": time.time() + 25 * 60}])
    out = report._workua_session_block()
    assert "жива" in out
    assert "хв" in out


def test_session_without_wsid_is_flagged(_settings, tmp_path):
    _write(tmp_path, [{"name": "_maau", "expires": time.time() + 9999}])
    out = report._workua_session_block()
    assert "wsid" in out
