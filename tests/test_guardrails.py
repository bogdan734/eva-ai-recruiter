from src.guardrails.exit import ExitReason, soft_exit_phrase, should_exit_now
from src.guardrails.profanity import contains_profanity, is_aggressive
from src.guardrails.repetition import RepetitionTracker


def test_profanity_detected():
    assert contains_profanity("ты дебил")
    assert contains_profanity("блядь, хватит")


def test_clean_text_passes():
    assert not contains_profanity("Доброго дня, мене звати Олена")
    assert not is_aggressive("Зараз не зручно, передзвоніть")


def test_aggression_marker():
    assert is_aggressive("заткнись, не звони мне")
    assert is_aggressive("йди на хер")


def test_repetition_tracker_detects_repeat():
    t = RepetitionTracker()
    assert t.observe("Скільки буде зарплата?") == 1
    assert t.observe("Я хочу знати графік") == 1
    assert t.observe("Яка зарплата буде?") == 2


def test_repetition_below_threshold():
    t = RepetitionTracker()
    assert t.observe("Розкажіть про вакансію") == 1
    assert t.observe("Який графік роботи?") == 1


def test_should_exit_aggression():
    t = RepetitionTracker()
    reason = should_exit_now("заткнись наконец", t, max_repetition=2)
    assert reason == ExitReason.AGGRESSIVE


def test_should_exit_repetition():
    t = RepetitionTracker()
    t.observe("Скільки буде зарплата")
    t.observe("Скільки буде зарплата")
    reason = should_exit_now("Яка зарплата буде", t, max_repetition=2)
    assert reason == ExitReason.REPETITIVE


def test_soft_exit_phrase_localized():
    uk = soft_exit_phrase(ExitReason.AGGRESSIVE, "uk")
    ru = soft_exit_phrase(ExitReason.AGGRESSIVE, "ru")
    en = soft_exit_phrase(ExitReason.AGGRESSIVE, "en")
    assert uk and ru and en
    assert uk != ru != en
