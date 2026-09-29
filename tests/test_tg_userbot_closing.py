"""Eva does not go silent on someone she has just turned down.

28.09: two candidates from Kyiv were told the vacancy is not for their region and
asked back "but you said it's remote — why not?" / "so there is no job now?".
Eva's own "not fit" had closed the conversation, the gate kept her quiet, nobody
was told, and the questions sat unread (12 and 8 messages) until the client
screenshotted them. Separately, a message that arrived while Eva was typing was
never answered at all: the history then ended on her reply and the model had
nothing to answer.
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
persona = _load("persona")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB", str(tmp_path / "tg.db"))


def test_eva_answers_after_turning_someone_down_herself():
    assert persona.closing_mode({"engage": False, "why": "closed"}, "not_fit", replies_since=0)


def test_and_after_the_deadline_closed_the_vacancy_for_them():
    # 29.09: "answer by 14:50, after that the vacancy is closed" — a late reply
    # gets the closing answer, not silence.
    assert persona.closing_mode({"engage": False, "why": "closed"}, "closed", replies_since=0)


def test_but_only_a_couple_of_times():
    assert not persona.closing_mode({"engage": False, "why": "closed"}, "not_fit", replies_since=2)


def test_a_recruiters_decision_still_keeps_her_quiet():
    assert not persona.closing_mode({"engage": False, "why": "final_stage"}, None, replies_since=0)
    assert not persona.closing_mode({"engage": False, "why": "closed"}, None, replies_since=0)
    assert not persona.closing_mode({"engage": False, "why": "recruiter_stage"}, "not_fit", replies_since=0)


def test_closing_note_goes_into_the_prompt():
    prompt = persona.system_prompt("BASE", False, closed=True)
    assert prompt.startswith("BASE") and "не підходить" in prompt
    assert persona.system_prompt("BASE", False) == "BASE"


def test_replies_are_counted_from_the_verdict_on(db):
    store.log_message("5", "user", "Київ")
    store.log_message("5", "assistant", "На жаль, ...")
    store.set_outcome("5", "not_fit")
    assert store.replies_since_outcome("5") == 0

    store.log_message("5", "user", "Чому не підходить?")
    store.log_message("5", "assistant", "Дякуємо за інтерес ...")
    assert store.replies_since_outcome("5") == 1


def test_a_message_sent_while_eva_typed_is_the_one_answered():
    msgs = [
        {"role": "assistant", "content": "Скільки років досвіду?"},
        {"role": "user", "content": "2 роки"},
        {"role": "user", "content": "А зарплата?"},
        {"role": "assistant", "content": "Дякую! Де ви проживаєте?"},
    ]

    fixed = persona.ends_on_candidate(msgs, "А зарплата?")

    assert fixed[-1] == {"role": "user", "content": "А зарплата?"}
    assert fixed[-2]["role"] == "assistant"
    assert len(fixed) == len(msgs)


def test_a_normal_history_is_left_alone():
    msgs = [{"role": "assistant", "content": "Привіт"}, {"role": "user", "content": "Так"}]
    assert persona.ends_on_candidate(msgs, "Так") == msgs
