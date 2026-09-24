"""The report must not announce a Cloudflare pause that has already expired."""
from datetime import datetime, timedelta

from src.bot.report import _first_future_stamp


def test_past_stamp_is_ignored():
    past = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    assert _first_future_stamp(past) is None


def test_future_stamp_is_returned():
    future = (datetime.utcnow() + timedelta(hours=2)).isoformat()
    assert _first_future_stamp(future) == future


def test_picks_the_future_one_out_of_a_mixed_pair():
    past = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    future = (datetime.utcnow() + timedelta(minutes=30)).isoformat()
    assert _first_future_stamp(past, future) == future


def test_none_and_garbage_are_survivable():
    assert _first_future_stamp(None, "", "not-a-date") is None
