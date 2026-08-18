"""The crash this guards against: a candidate who applied through two channels.

`candidates.source` accumulated tags by string concatenation into a VARCHAR(32)
column, so the second channel already overflowed it —
"workua_response_send,workua_response_phonecall" is 45 characters. The whole
ingest transaction died with StringDataRightTruncationError, which is how the
work.ua catch-up replay hit a wall on 2026-08-18. The old membership test was a
substring check too, so a tag that happened to be a prefix of another was
silently treated as already recorded.
"""
from src.api.inbound_router import SOURCE_MAX_LEN, merge_sources


def test_a_second_channel_is_appended():
    assert (
        merge_sources("workua_response_send", "workua_response_phonecall")
        == "workua_response_send,workua_response_phonecall"
    )


def test_the_same_channel_twice_changes_nothing():
    assert merge_sources("robotaua_response", "robotaua_response") == "robotaua_response"


def test_a_tag_that_is_a_prefix_of_another_is_still_recorded():
    """Substring matching used to swallow this one."""
    assert (
        merge_sources("robotaua_chat_v2", "robotaua_chat")
        == "robotaua_chat_v2,robotaua_chat"
    )


def test_every_real_channel_fits_within_the_column():
    tags = [
        "workua_response_send",
        "workua_response_phonecall",
        "robotaua_response",
        "robotaua_chat",
    ]
    merged = ""
    for t in tags:
        merged = merge_sources(merged, t)
    assert merged.split(",") == tags
    assert len(merged) <= SOURCE_MAX_LEN


def test_overflow_drops_whole_tags_never_half_of_one():
    merged = merge_sources("workua_response_send", "workua_response_phonecall", limit=25)
    assert merged == "workua_response_send"
    assert all(len(t) > 0 for t in merged.split(","))


def test_empty_sides_are_tolerated():
    assert merge_sources("", "manual") == "manual"
    assert merge_sources("manual", "") == "manual"
    assert merge_sources(None, None) == ""
