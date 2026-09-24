"""Daily report rendering.

Rewritten 09.09.2026: the old section test still built DayReport with fields
from an earlier design (success / no_answer / hangup / blocked /
avg_success_sec / cost_breakdown) and had been failing with a TypeError ever
since the report was reworked around people rather than attempts.
"""
from datetime import date

from src.bot.report import DayReport, _fmt_duration, format_report_md


def test_duration_under_minute():
    assert _fmt_duration(45) == "00:45"


def test_duration_minutes():
    assert _fmt_duration(222) == "03:42"


def test_duration_hours():
    assert _fmt_duration(4995) == "1:23:15"


def test_duration_zero_and_negative_are_safe():
    assert _fmt_duration(0) == "00:00"
    assert _fmt_duration(-5) == "00:00"


def _sample() -> DayReport:
    return DayReport(
        target_date=date(2026, 9, 8),
        people_dialed=47,
        people_talked=12,
        qualified=4,
        rejected=6,
        dropped_early=2,
        unreachable=28,
        not_connected=5,
        attempts=61,
        avg_talk_sec=222,
        total_in_line_sec=4995,
        cost={"claude": 4.21, "vapi": 2.10, "total": 6.31},
        intake_by_source={"workua_response_send": 10, "robotaua_response": 5},
        tg_sent_today=3,
        tg_limit=25,
        tg_active=True,
        funnel_to_call=0,
        funnel_calling=0,
        funnel_manager=589,
        funnel_rejected=179,
        funnel_unreachable=8,
        qualified_names=["Іван Петренко"],
    )


def test_report_includes_all_sections():
    md = format_report_md(_sample())
    for section in ("Нові кандидати", "Воронка", "Витрати", "Telegram"):
        assert section in md, f"missing section: {section}"


def test_report_shows_the_numbers_it_was_given():
    md = format_report_md(_sample())
    assert "589" in md          # у рекрутера
    assert "179" in md          # не підійшли
    assert "Іван Петренко" in md


def test_report_survives_a_completely_empty_day():
    """Every counter at zero is the normal shape of a quiet day, not an error --
    the report must still render (this is what the whole of 05-09.09 looked
    like, with the dialer idle)."""
    md = format_report_md(DayReport(target_date=date(2026, 9, 9)))
    assert md
    assert "Воронка" in md
