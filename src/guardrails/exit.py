"""Soft-exit phrase library — polite ways to end a call under different triggers.

Used by the orchestrator after a guardrail trip. Multilingual (uk/ru/en) — pick
based on `language_used` detected in the call.
"""
from __future__ import annotations

from enum import Enum

from .profanity import is_aggressive
from .repetition import RepetitionTracker


class ExitReason(str, Enum):
    AGGRESSIVE = "aggressive"
    REPETITIVE = "repetitive"
    FORBIDDEN_TOPIC = "forbidden_topic"
    CANDIDATE_REFUSED = "candidate_refused"
    CONSENT_DENIED = "consent_denied"
    MAX_DURATION = "max_duration"


SOFT_EXIT_PHRASES: dict[str, dict[ExitReason, str]] = {
    "uk": {
        ExitReason.AGGRESSIVE: (
            "Я бачу, що зараз не найкращий час. Дякую за приділений час, гарного дня."
        ),
        ExitReason.REPETITIVE: (
            "Дякую за уточнення. Передам ваші питання менеджеру, він з вами звʼяжеться. До побачення."
        ),
        ExitReason.FORBIDDEN_TOPIC: (
            "Це питання краще обговорити з менеджером. Дякую за розмову, гарного дня."
        ),
        ExitReason.CANDIDATE_REFUSED: (
            "Зрозуміла. Дякую за відверту відповідь і за приділений час. Гарного дня!"
        ),
        ExitReason.CONSENT_DENIED: (
            "Дякую, я завершую дзвінок. Гарного дня."
        ),
        ExitReason.MAX_DURATION: (
            "На жаль, я мушу завершувати. Передам інформацію менеджеру і він з вами звʼяжеться."
        ),
    },
    "ru": {
        ExitReason.AGGRESSIVE: (
            "Вижу, сейчас не лучшее время. Спасибо за уделённое время, хорошего дня."
        ),
        ExitReason.REPETITIVE: (
            "Спасибо за уточнения. Передам ваши вопросы менеджеру, он свяжется. До свидания."
        ),
        ExitReason.FORBIDDEN_TOPIC: (
            "Этот вопрос лучше обсудить с менеджером. Спасибо за разговор, хорошего дня."
        ),
        ExitReason.CANDIDATE_REFUSED: (
            "Поняла. Спасибо за откровенный ответ и уделённое время. Хорошего дня!"
        ),
        ExitReason.CONSENT_DENIED: (
            "Спасибо, я завершаю звонок. Хорошего дня."
        ),
        ExitReason.MAX_DURATION: (
            "К сожалению, мне нужно заканчивать. Передам информацию менеджеру."
        ),
    },
    "en": {
        ExitReason.AGGRESSIVE: "I can see this isn't the best time. Thank you for your time, have a good day.",
        ExitReason.REPETITIVE: "Thanks for the questions. I'll pass them to our manager. Goodbye.",
        ExitReason.FORBIDDEN_TOPIC: "Let's leave this question for our manager. Thank you, have a good day.",
        ExitReason.CANDIDATE_REFUSED: "Understood. Thanks for the honest answer. Have a good day!",
        ExitReason.CONSENT_DENIED: "Thank you, I'll end the call. Have a good day.",
        ExitReason.MAX_DURATION: "I need to wrap up. Our manager will be in touch with the details.",
    },
}


def soft_exit_phrase(reason: ExitReason, language: str = "uk") -> str:
    lang = language if language in SOFT_EXIT_PHRASES else "uk"
    return SOFT_EXIT_PHRASES[lang][reason]


def should_exit_now(
    utterance: str,
    repetition_tracker: RepetitionTracker,
    max_repetition: int,
) -> ExitReason | None:
    if is_aggressive(utterance):
        return ExitReason.AGGRESSIVE
    rep_count = repetition_tracker.observe(utterance)
    if rep_count > max_repetition:
        return ExitReason.REPETITIVE
    return None
