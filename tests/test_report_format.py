from datetime import date

from src.bot.report import DayReport, _fmt_duration, format_report_md


def test_duration_under_minute():
    assert _fmt_duration(45) == "00:45"


def test_duration_minutes():
    assert _fmt_duration(222) == "03:42"


def test_duration_hours():
    assert _fmt_duration(4995) == "1:23:15"


def test_report_includes_all_sections():
    rep = DayReport(
        target_date=date(2026, 6, 19),
        attempts=47,
        success=12,
        no_answer=28,
        hangup=5,
        blocked=2,
        qualified=4,
        avg_success_sec=222,
        total_in_line_sec=4995,
        cost_breakdown={
            "claude_usd": 4.21,
            "deepgram_usd": 0.62,
            "elevenlabs_usd": 1.84,
            "vapi_usd": 4.10,
            "telephony_usd": 3.30,
            "total_usd": 14.07,
        },
        scraper_new=84,
        scraper_filtered=31,
        scraper_queued=27,
        funnel_to_call=142,
        funnel_calling=8,
        funnel_manager=4,
        funnel_archive_today=17,
    )
    out = format_report_md(rep)
    assert "Звіт за 19.06.2026" in out
    assert "Спроб: 47" in out
    assert "Успішні: 12" in out
    assert "Кваліфіковано: 4" in out
    assert "$14.07" in out
    assert "Нових резюме: 84" in out
    assert "To call: 142" in out
