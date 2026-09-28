"""A candidate Eva may not answer must still reach a human.

When a recruiter owns the card, the tg-gate keeps Eva quiet — and until 28.09
that was the end of it: the reply sat in Eva's Telegram, which no recruiter
reads. All three people who answered the September outreach waited there for
days. The userbot now forwards what they wrote, once per new message.
"""
import importlib.util
from pathlib import Path

import pytest

_USERBOT = Path(__file__).resolve().parents[1] / "tg_userbot"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"tg_userbot_{name}", _USERBOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = _load("store")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB", str(tmp_path / "tg.db"))


def test_tail_is_what_the_candidate_wrote_after_evas_last_word(db):
    store.log_message("7", "user", "Добрий день")
    store.log_message("7", "assistant", "Доброго дня! Це Єва.")
    store.log_message("7", "user", "Так, ще цікавить")
    store.log_message("7", "user", "  ")
    store.log_message("7", "user", "Розкажіть умови")

    newest, texts = store.unanswered_tail("7")

    assert texts == ["Так, ще цікавить", "Розкажіть умови"]
    assert newest == 5


def test_nothing_is_owed_once_eva_has_answered(db):
    store.log_message("7", "user", "Привіт")
    store.log_message("7", "assistant", "Вітаю!")

    assert store.unanswered_tail("7") == (None, [])


def test_one_alert_per_new_message(db):
    store.log_message("7", "user", "Так, ще цікавить")
    first, _ = store.unanswered_tail("7")
    assert not store.silenced_alert_sent("7", first)

    store.mark_silenced_alert("7", first)
    assert store.silenced_alert_sent("7", first)

    store.log_message("7", "user", "Коли можна поговорити?")
    newer, texts = store.unanswered_tail("7")
    assert newer > first
    assert not store.silenced_alert_sent("7", newer)
    assert texts == ["Так, ще цікавить", "Коли можна поговорити?"]


def test_alerts_are_tracked_per_peer(db):
    store.log_message("7", "user", "Так")
    store.log_message("8", "user", "Так")
    msg7, _ = store.unanswered_tail("7")
    store.mark_silenced_alert("7", msg7)

    msg8, _ = store.unanswered_tail("8")
    assert not store.silenced_alert_sent("8", msg8)
