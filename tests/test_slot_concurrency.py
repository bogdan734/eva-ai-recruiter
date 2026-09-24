"""A slot must never dial more lines than the trunk carries.

Pins the 23.09.2026 fix. The failure it prevents is invisible from our side:
the carrier answers 486 Busy here, Vapi files it as failed-to-connect, and the
candidate never learns we called. Three of five calls died that way on 20.09.
"""
import inspect

from src.scheduler import dispatcher
from src.common.settings import get_settings


def test_batch_asks_how_many_lines_are_free():
    src = inspect.getsource(dispatcher.run_slot)
    assert "_calls_in_flight" in src, (
        "a batch must count live calls before dialling, not trust a fixed pause"
    )
    assert "call_max_concurrent" in src


def test_the_count_comes_before_the_dial():
    src = inspect.getsource(dispatcher.run_slot)
    i_count = src.find("_calls_in_flight")
    i_dial = src.find("dispatch_for_candidate")
    assert i_count != -1 and i_dial != -1
    assert i_count < i_dial, "counting after dialling protects nobody"


def test_in_flight_helper_bounds_by_age():
    """An unfinalised row must not pin a phantom channel forever."""
    src = inspect.getsource(dispatcher._calls_in_flight)
    assert "ended_at" in src
    assert "cutoff" in src or "max_age_sec" in src


def test_concurrency_never_exceeds_the_trunk():
    """Stream Telecom carries 5 simultaneous calls (confirmed by them 23.09.2026).

    Dialling a sixth does not queue -- the trunk answers 486 Busy here, Vapi
    files it as failed-to-connect, and the candidate's phone never rings. Raise
    this only together with the carrier's channel count.
    """
    TRUNK_CHANNELS = 5
    assert get_settings().call_max_concurrent <= TRUNK_CHANNELS, (
        "more concurrent calls than the trunk has channels -- the extra ones "
        "are silently lost, which is exactly the 20-22.09 failure"
    )
