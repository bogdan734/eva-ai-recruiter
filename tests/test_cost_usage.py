from src.cost.pricing import claude_cost
from src.cost.summary import SpendReport
from datetime import datetime


def test_off_call_tokens_change_the_anthropic_figure():
    """The bug this fixes: only call tokens were priced, so /costs understated."""
    call_in, call_out = 10_000, 2_000
    off_in, off_out = 40_000, 3_000

    only_calls = claude_cost(call_in, call_out, cheap=True)
    with_everything = claude_cost(call_in + off_in, call_out + off_out, cheap=True)

    assert with_everything > only_calls
    # name_origin runs on every intake, so off-call volume dominates in practice.
    assert with_everything > only_calls * 2


def test_report_keeps_off_call_tokens_separate():
    r = SpendReport(
        since=datetime(2026, 8, 4),
        tokens_in=100,
        tokens_out=20,
        off_call_tokens_in=500,
        off_call_tokens_out=60,
    )
    # Kept apart so /costs can show where the bill goes, not just a lump sum.
    assert r.tokens_in == 100
    assert r.off_call_tokens_in == 500
    assert r.tokens_in + r.off_call_tokens_in == 600


def test_total_sums_per_service():
    r = SpendReport(since=datetime(2026, 8, 4))
    r.per_service = {"vapi": 1.5, "anthropic": 2.25, "deepgram": 0.1}
    assert r.total == 3.85
