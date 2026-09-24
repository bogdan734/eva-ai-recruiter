"""A second Google-Form application must not vanish into a comment.

Pins the 23.09.2026 request: every submission is its own card on «Новий», with
the buyer left unsaved -- while a repeat BOARD response keeps folding into the
card it already has, which is a different rule on purpose.
"""
import inspect

from src.api import inbound_router


def test_googleform_is_listed_as_always_new_card():
    assert "googleform" in inbound_router.ALWAYS_NEW_CARD_SOURCES


def test_board_sources_are_not():
    for s in ("workua_response_send", "robotaua_response", "workua_search", "telegram"):
        assert s not in inbound_router.ALWAYS_NEW_CARD_SOURCES, (
            f"{s} must keep folding a repeat into the existing card"
        )


def test_the_duplicate_branch_checks_the_flag_before_commenting():
    """The flag has to be read BEFORE _note_repeat_response, not after.

    Read in the wrong order it would still comment and still return
    local_duplicate, and the new card would never be reached -- the exact
    failure this change exists to prevent.
    """
    src = inspect.getsource(inbound_router.InboundRouter.ingest)
    i_flag = src.find("ALWAYS_NEW_CARD_SOURCES")
    i_note = src.find("_note_repeat_response")
    assert i_flag != -1 and i_note != -1
    assert i_flag < i_note, "the always-new-card check must come first"


def test_cards_are_still_created_with_the_buyer_unsaved():
    src = inspect.getsource(inbound_router.InboundRouter.ingest)
    assert "save_buyer=False" in src
