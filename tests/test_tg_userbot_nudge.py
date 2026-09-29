"""When Eva reminds a silent candidate, and when she lets the card go."""
import importlib.util
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_USERBOT = Path(__file__).resolve().parents[1] / "tg_userbot"
H = 3600


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"tg_userbot_{name}", _USERBOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = _load("store")
persona = _load("persona")
KYIV = ZoneInfo("Europe/Kyiv")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB", str(tmp_path / "tg.db"))


def _step(**kw):
    now = 1_000_000.0
    args = dict(last_role="assistant", last_ts=now - 49 * H, reminded_at=None,
                closed_at=None, now=now)
    args.update({k: (now - v if k in ("last_ts", "reminded_at", "closed_at") and v is not None else v)
                 for k, v in kw.items()})
    return persona.silence_step(**args)


def test_two_silent_days_bring_one_reminder():
    assert _step() == "remind"
    assert _step(last_ts=47 * H) is None


def test_three_more_days_after_the_reminder_close_the_card():
    assert _step(last_ts=73 * H, reminded_at=73 * H) == "close"
    assert _step(last_ts=71 * H, reminded_at=71 * H) is None


def test_nothing_while_the_candidate_has_the_last_word():
    assert _step(last_role="user") is None


def test_nothing_after_the_card_was_let_go():
    assert _step(last_ts=100 * H, reminded_at=100 * H, closed_at=20 * H) is None


def test_reminders_go_out_in_daytime_on_working_days():
    assert persona.nudge_hours(datetime(2026, 9, 30, 12, 0, tzinfo=KYIV))       # Wednesday
    assert persona.nudge_hours(datetime(2026, 10, 3, 11, 0, tzinfo=KYIV))       # Saturday
    assert not persona.nudge_hours(datetime(2026, 10, 4, 12, 0, tzinfo=KYIV))   # Sunday
    assert not persona.nudge_hours(datetime(2026, 9, 30, 20, 0, tzinfo=KYIV))
    assert not persona.nudge_hours(datetime(2026, 9, 30, 8, 0, tzinfo=KYIV))


def test_the_reminder_asks_whether_it_is_still_relevant():
    assert "актуальн" in persona.REMINDER_TEXT


def _put(peer, role, text, ts):
    c = store._conn()
    c.execute("INSERT INTO messages(peer,role,text,ts) VALUES(?,?,?,?)", (peer, role, text, ts))
    c.commit(); c.close()


def test_silent_dialogs_are_the_ones_eva_ended_two_days_ago_or_more(db):
    now = time.time()
    _put("1", "user", "Добрий день, розкажіть умови", now - 60 * H)   # talked, Eva last 50h ago
    _put("1", "assistant", "Скільки років досвіду?", now - 50 * H)
    _put("2", "assistant", "Скільки років досвіду?", now - 60 * H)   # candidate has the last word
    _put("2", "user", "Два", now - 50 * H)
    _put("3", "assistant", "Доброго дня! Це Єва", now - 60 * H)      # a chat nobody answered yet
    _put("4", "user", "Так", now - 30 * 24 * H)                      # went quiet a month ago
    _put("4", "assistant", "Де ви живете?", now - 20 * 24 * H)
    _put("5", "user", "Так", now - 30 * H)                           # Eva spoke only a day ago
    _put("5", "assistant", "Де ви живете?", now - 24 * H)

    found = {peer for peer, _ in store.silent_dialogs(older_than=now - 48 * H,
                                                      newer_than=now - 14 * 24 * H)}

    assert found == {"1", "3"}


def test_nudge_state_round_trip(db):
    assert store.nudge_state("7") == (None, None)
    store.mark_reminded("7")
    reminded, closed = store.nudge_state("7")
    assert reminded and closed is None
    store.mark_silence_closed("7")
    assert all(store.nudge_state("7"))
    store.clear_nudge("7")
    assert store.nudge_state("7") == (None, None)
