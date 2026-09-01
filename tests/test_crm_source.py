"""Which «Джерело» a card gets in KeyCRM.

Every card we have ever created carries source_id=1, the default in create_lead,
which in this account means work.ua. Nobody passed anything else, so a candidate
who applied on robota.ua was filed under work.ua — and the recruiter filtering
the funnel by robota.ua saw an empty list and reported that robota.ua leads were
missing. They were not missing; they were mislabelled.

The ids are this account's own, read off the cabinet on 2026-08-18 and confirmed
against card contents: 1 work.ua, 2 rabota.ua, 3 Анкети, 4 Telegram.
"""
import pytest

from src.common.keycrm import DEFAULT_SOURCE_ID, crm_source_id


class TestChannelMapping:
    @pytest.mark.parametrize(
        "source,expected",
        [
            ("workua_response_send", 1),
            ("workua_response_phonecall", 1),
            ("robotaua_response", 2),
            ("robotaua_chat", 2),
            ("robotaua", 2),
            ("telegram", 4),
        ],
    )
    def test_known_channels(self, source, expected):
        assert crm_source_id(source) == expected


class TestCombinedSources:
    """The column accumulates channels as a person reappears, comma-joined."""

    def test_first_recognised_channel_wins(self):
        """A card is created once, at first contact — the origin is what counts."""
        assert crm_source_id("robotaua_response,workua_response_send") == 2
        assert crm_source_id("workua_response_send,robotaua_response") == 1

    def test_skips_unknown_leading_token(self):
        assert crm_source_id("inbound_call,robotaua_response") == 2


class TestFallback:
    @pytest.mark.parametrize("source", [None, "", "   ", "inbound_call", "хтозна"])
    def test_unknown_falls_back_to_default(self, source):
        assert crm_source_id(source) == DEFAULT_SOURCE_ID

    def test_default_is_workua(self):
        """Changing this silently re-labels every card whose channel we cannot read."""
        assert DEFAULT_SOURCE_ID == 1


class TestRobustness:
    def test_case_and_padding_do_not_matter(self):
        assert crm_source_id("  RobotaUA_Response  ") == 2

    def test_never_returns_a_foreign_only_source(self):
        """3 (Анкети) belongs to the other integration — we must never claim it."""
        for src in ("workua_response_send", "robotaua_response", "telegram", "x"):
            assert crm_source_id(src) != 3
