"""Regression test for the 2026-09-03 orphan-card bug in
scripts/bulk_push_workua_gap.py: push_one() used to catch a local
phone_e164 collision after a successful KeyCRM card creation, then silently
report success anyway -- no local row, no error, no trace. 90 cards from
this exact script went unnoticed for hours because of it (every later
archiving/dedup script queries via the local table, so an unlinked card was
invisible to all of them).

These tests cover the pure decision logic (resolve_collision) and the
durable fallback log (record_orphan_card) without touching a real database,
matching how the rest of this codebase tests pure functions.
"""
import json

from scripts.bulk_push_workua_gap import record_orphan_card, resolve_collision


def test_resolve_collision_attaches_to_a_free_row():
    # existing local row has no keycrm_lead_id yet -- safe to claim it.
    assert resolve_collision(None) == "attach"
    assert resolve_collision(0) == "attach"


def test_resolve_collision_orphans_when_row_already_claimed():
    # existing local row already points at a different card -- attaching
    # would silently lose track of THAT card instead. Must not attach.
    assert resolve_collision(12345) == "orphan"


def test_record_orphan_card_is_durable_and_appendable(tmp_path):
    log_path = tmp_path / "orphan_keycrm_cards.jsonl"
    record_orphan_card(101, "Тест Тестович", "+380991112233", "phone_e164 вже зайнятий", log_path=log_path)
    record_orphan_card(102, "Другий Кандидат", "+380991112244", "IntegrityError", log_path=log_path)

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["lead_id"] == 101
    assert first["full_name"] == "Тест Тестович"
    assert first["phone"] == "+380991112233"
    assert "reason" in first and "recorded_at" in first

    second = json.loads(lines[1])
    assert second["lead_id"] == 102


def test_record_orphan_card_creates_parent_directory(tmp_path):
    log_path = tmp_path / "nested" / "state" / "orphan_keycrm_cards.jsonl"
    record_orphan_card(1, "X", "+380990000000", "test", log_path=log_path)
    assert log_path.exists()
