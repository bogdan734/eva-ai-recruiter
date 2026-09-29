"""A call that ends half-way must leave Eva something she will actually do next.

25.09: bad line, the candidate said "call me in half an hour", Eva said she would.
The verdict was "promising, finish in Telegram" -- that branch won over the
callback, the chat never went out (no Telegram on the number), and the card sat
in «В роботі» with nobody on it.
"""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.call.orchestrator import _chat_fallback, _potential_fit_plan
from src.common.models import CandidateStatus

KYIV = ZoneInfo("Europe/Kyiv")


def _summary(text: str, when: str | None = None):
    return SimpleNamespace(summary=text, best_callback_time=when)


def test_a_promised_callback_wins_over_the_chat():
    status, callback_at, chat = _potential_fit_plan(
        _summary("• Постійні проблеми зі звʼязком • Домовлено передзвонити через 30 хвилин"), KYIV)

    assert status == CandidateStatus.IN_CALL_QUEUE
    assert callback_at is not None and callback_at > datetime.now(KYIV)
    assert chat is None


def test_without_a_callback_the_screening_moves_to_telegram():
    status, callback_at, chat = _potential_fit_plan(
        _summary("• Досвід у продажах 2 роки • Регіон і вік не зібрані"), KYIV)

    assert (status, callback_at, chat) == (CandidateStatus.CALL_DONE, None, "collect_info")


def test_no_telegram_on_the_number_means_eva_calls_back():
    status, callback_at = _chat_fallback(_summary("• Регіон і вік не зібрані"), KYIV)

    assert status == CandidateStatus.IN_CALL_QUEUE
    assert callback_at is not None and callback_at > datetime.now(KYIV)
