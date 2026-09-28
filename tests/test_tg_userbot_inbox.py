"""A voice message, a sticker or a photo without a caption must not end the chat.

Telethon gives such a message an empty `raw_text`. The userbot stored it as is,
and the API refuses a conversation with an empty turn — so every later reply to
that person failed, and Eva went silent for good after one voice note.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_USERBOT = Path(__file__).resolve().parents[1] / "tg_userbot"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"tg_userbot_{name}", _USERBOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


persona = _load("persona")
store = _load("store")


def _message(raw_text="", **media):
    fields = dict.fromkeys(("voice", "video_note", "sticker", "photo", "document"))
    fields.update(media)
    return SimpleNamespace(raw_text=raw_text, **fields)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB", str(tmp_path / "tg.db"))


def test_history_leaves_out_empty_messages(db):
    store.log_message("42", "assistant", "Доброго дня! Це Єва.")
    store.log_message("42", "user", "")
    store.log_message("42", "user", "Львів")
    store.log_message("42", "user", "   ")

    assert store.history("42") == [
        {"role": "assistant", "content": "Доброго дня! Це Єва."},
        {"role": "user", "content": "Львів"},
    ]
    assert store.history("42", limit=1) == [{"role": "user", "content": "Львів"}]


@pytest.mark.parametrize(
    ("media", "expected"),
    [
        ({"voice": object(), "document": object()}, "[голосове повідомлення]"),
        ({"video_note": object(), "document": object()}, "[відеоповідомлення]"),
        ({"sticker": object(), "document": object()}, "[стікер]"),
        ({"photo": object()}, "[фото]"),
        ({"document": object()}, "[файл]"),
    ],
)
def test_media_without_caption_reaches_eva_as_a_placeholder(media, expected):
    assert persona.incoming_text(_message(**media)) == expected


def test_caption_wins_over_the_placeholder():
    assert persona.incoming_text(_message("  Моє резюме ", document=object())) == "Моє резюме"


def test_nothing_readable_gives_empty_text():
    assert persona.incoming_text(_message(None)) == ""


def test_prompt_tells_eva_what_each_placeholder_means():
    for placeholder in ("[голосове повідомлення]", "[відеоповідомлення]", "[стікер]",
                        "[фото]", "[файл]"):
        assert placeholder in persona.SYSTEM_PROMPT


def test_prompt_forbids_inventing_terms_it_does_not_have():
    # 28.09 Eva told a candidate "офіційне влаштування за трудовим договором" —
    # nothing in her instructions says so. Terms she was not given go to the recruiter.
    assert "оформлення" in persona.SYSTEM_PROMPT
    assert "розповість рекрутер на співбесіді" in persona.SYSTEM_PROMPT


def test_paused_hiring_prompt_offers_the_reserve_not_the_recruiter():
    # 28.09: hiring is paused — Eva says so and offers the talent reserve instead of
    # promising a recruiter's call. Switching the flag off gives the plain prompt back.
    paused = persona.system_prompt("BASE", paused=True)

    assert paused.startswith("BASE")
    assert "кадрового резерву" in paused
    assert "НЕ кажи «передаю" in paused
    assert persona.system_prompt("BASE", paused=False) == "BASE"
