"""Nobody becomes a saved «Покупець» without a human choosing it.

`create_lead(save_buyer=False)` has been the rule since 03.09, but the post-call
code kept calling `ensure_buyer` (find-*or-create*) so it could write its
"called? / result" note — so every call quietly saved a buyer anyway. Visible on
card 11407 from the 17.09 test call: no buyer at creation, a green «Покупець»
half a second later.
"""
import inspect
import re

from src.api import tg_outcome
from src.call import orchestrator
from src.common.keycrm import KeyCRMClient


def _calls_ensure_buyer(module) -> bool:
    """Is ensure_buyer actually invoked here? A mention in a comment does not
    count — what matters is whether a buyer can come into existence."""
    return bool(re.search(r"ensure_buyer\s*\(", inspect.getsource(module)))


def test_create_lead_still_defaults_to_not_saving_a_buyer():
    default = inspect.signature(KeyCRMClient.create_lead).parameters["save_buyer"].default
    assert default is False


def test_the_post_call_path_never_creates_a_buyer():
    assert not _calls_ensure_buyer(orchestrator), (
        "ensure_buyer creates a buyer when none exists — the post-call note must "
        "use find_buyer_by_phone and skip when the person is not saved"
    )
    assert "find_buyer_by_phone" in inspect.getsource(orchestrator)


def test_the_telegram_outcome_path_never_creates_a_buyer():
    assert not _calls_ensure_buyer(tg_outcome)
    assert "find_buyer_by_phone" in inspect.getsource(tg_outcome)


def test_ensure_buyer_still_exists_for_explicit_use():
    """Not deleted — it is the right call when somebody deliberately asks to save
    a client. It just must not run on its own after every call."""
    assert hasattr(KeyCRMClient, "ensure_buyer")
