"""The report's day is the Kyiv day, not the UTC one.

10.09.2026: the morning report said "Нові кандидати: 0" while a candidate had
arrived at 00:25 Kyiv — that hour belongs to the previous UTC day, so it was
counted against yesterday and the day looked dead.
"""
from datetime import date, timedelta

from src.bot.report import _kyiv_day_bounds


def test_day_starts_at_kyiv_midnight_not_utc_midnight():
    start, end = _kyiv_day_bounds(date(2026, 9, 10))
    # Kyiv is UTC+3 in September, so the day opens at 21:00 UTC the day before.
    assert start.utcoffset() == timedelta(hours=3)
    assert start.astimezone(tz=None).tzinfo is not None
    assert start.isoformat().startswith("2026-09-10T00:00:00")


def test_range_is_exactly_one_day_and_half_open():
    start, end = _kyiv_day_bounds(date(2026, 9, 10))
    assert end - start == timedelta(days=1)
    assert end.isoformat().startswith("2026-09-11T00:00:00")


def test_an_after_midnight_kyiv_candidate_falls_inside_its_own_day():
    """00:25 Kyiv on the 10th = 21:25 UTC on the 9th -- the exact record that
    went missing from the report."""
    from datetime import datetime, timezone

    start, end = _kyiv_day_bounds(date(2026, 9, 10))
    created_utc = datetime(2026, 9, 9, 21, 25, tzinfo=timezone.utc)
    assert start <= created_utc < end
