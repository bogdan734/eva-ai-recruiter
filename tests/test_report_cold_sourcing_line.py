"""The morning report has to distinguish "blocked" from "nobody matched"."""
import json
import time

import pytest

from src.bot import report


@pytest.fixture
def _state(monkeypatch, tmp_path):
    class _S:
        workua_session_state_path = str(tmp_path / "workua_session.json")

    monkeypatch.setattr("src.common.settings.get_settings", lambda: _S())

    def write(payload):
        (tmp_path / "workua_cold_sourcing.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    return write


def test_never_run_says_so(_state):
    assert "ще не запускався" in report._cold_sourcing_last_run_line()


def test_challenged_run_names_the_cause_and_the_fix(_state):
    _state({"outcome": "challenged", "at": time.time()})
    line = report._cold_sourcing_last_run_line()
    assert "бот-перевірку" in line
    assert "проксі" in line


def test_missing_session_is_distinct_from_a_block(_state):
    _state({"outcome": "no_session", "at": time.time()})
    line = report._cold_sourcing_last_run_line()
    assert "не було сесії" in line
    assert "проксі" not in line


def test_successful_run_reports_the_counts(_state):
    _state({
        "outcome": "ok",
        "at": time.time(),
        "urls_found": 6,
        "with_phone": 4,
        "after_region_filter": 2,
    })
    line = report._cold_sourcing_last_run_line()
    assert "переглянуто 6" in line
    assert "відкрито контактів 4" in line
    assert "пройшли відбір 2" in line


def test_a_quiet_but_working_run_is_not_reported_as_a_block(_state):
    """Zero found with outcome=ok means work.ua answered and nobody matched --
    that must not read as a block, or a proxy gets bought for nothing."""
    _state({"outcome": "ok", "at": time.time(), "urls_found": 0, "with_phone": 0,
            "after_region_filter": 0})
    line = report._cold_sourcing_last_run_line()
    assert "переглянуто 0" in line
    assert "проксі" not in line
