"""The digest line that says whether last night's dump happened.

A backup nobody checks is worse than no backup: it buys confidence it has not
earned. The line is therefore judged on its alarms, not its happy path — a
silent green when the cron has been dead for a week would repeat the mistake
this project keeps making, a counter at zero that nobody reads as a symptom.
"""
import json
from datetime import UTC, datetime, timedelta

import pytest

from src.bot import report as report_mod


@pytest.fixture
def status_file(tmp_path, monkeypatch):
    """Point the block at a throwaway state dir."""
    import src.common.state as state_mod

    monkeypatch.setattr(state_mod, "state_dir", lambda: tmp_path)
    return tmp_path / "backup_status.json"


def _write(path, *, ok=True, age_hours=2, size=500_000, error=""):
    at = datetime.now(UTC) - timedelta(hours=age_hours)
    path.write_text(
        json.dumps(
            {
                "ok": ok,
                "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "file": "backups/x.sql.gz",
                "bytes": size,
                "error": error,
            }
        ),
        encoding="utf-8",
    )


class TestAlarms:
    """Every way a backup can be broken has to show red."""

    def test_never_run_is_loud(self, status_file):
        assert "🔴" in report_mod._backup_block()

    def test_failed_run_is_loud_and_says_why(self, status_file):
        _write(status_file, ok=False, error="connection refused")
        out = report_mod._backup_block()
        assert "🔴" in out
        assert "connection refused" in out

    def test_stale_backup_is_loud(self, status_file):
        """Cron runs nightly — two days of silence means it stopped."""
        _write(status_file, ok=True, age_hours=50)
        out = report_mod._backup_block()
        assert "🔴" in out
        assert "крон" in out.lower() or "год" in out

    def test_unreadable_status_is_loud(self, status_file):
        status_file.write_text("{ not json", encoding="utf-8")
        assert "🔴" in report_mod._backup_block()


class TestHealthy:
    def test_fresh_backup_is_quiet(self, status_file):
        _write(status_file, ok=True, age_hours=3)
        out = report_mod._backup_block()
        assert "🔴" not in out

    def test_shows_size_and_time(self, status_file):
        _write(status_file, ok=True, age_hours=3, size=497_716)
        out = report_mod._backup_block()
        assert "0.5" in out
        assert "UTC" in out

    def test_boundary_just_inside_the_window(self, status_file):
        _write(status_file, ok=True, age_hours=35)
        assert "🔴" not in report_mod._backup_block()
