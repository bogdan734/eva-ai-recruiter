from src.cost.pricing import call_cost, claude_cost


def test_claude_sonnet_cost():
    # 1M in + 1M out = $3 + $15 = $18
    assert claude_cost(1_000_000, 1_000_000) == 18.0


def test_claude_haiku_cost():
    # 1M in + 1M out = $1 + $5 = $6
    assert claude_cost(1_000_000, 1_000_000, cheap=True) == 6.0


def test_call_cost_breakdown_present():
    cost = call_cost(duration_min=3.0, tokens_in=2000, tokens_out=400)
    assert "claude" in cost
    assert "deepgram" in cost
    assert "elevenlabs" in cost
    assert "vapi" in cost
    assert "telephony" in cost
    assert "total" in cost
    assert cost["total"] > 0


def test_call_cost_roughly_matches_planning():
    # ~3-minute call should be ~$0.50–0.70 (per our budget plan)
    cost = call_cost(duration_min=3.0, tokens_in=8000, tokens_out=1500)
    assert 0.3 < cost["total"] < 1.5
