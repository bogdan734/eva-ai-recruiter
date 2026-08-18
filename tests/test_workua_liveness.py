"""The silence this guards against: a posting the client deletes.

A deleted vacancy produces no error, no responses and no signal of any kind —
the poller keeps reporting new=0 errors=0 and a fixed intake over a dead source
looks exactly like a working one. That is how both sales postings went away on
2026-08-13 and nobody knew until 18.08, with the call queue starving the whole
time. `unknown_vacancies` cannot catch this: it fires on a REPUBLISHED id
arriving, and a deleted posting never sends anything at all.

The other half of the job is not crying wolf. work.ua being briefly unreachable
must never read as "deleted" — an alert that fires on a timeout gets muted by
whoever receives it, and then the real one is muted too.
"""
import pytest

from src.integrations import workua_liveness as wl

REMOVED_BODY = (
    "<h1>Вакансія була видалена або прихована роботодавцем. Проте є інші</h1>"
)


def test_a_live_posting_is_alive():
    assert (
        wl.classify(
            8242731, 200, "https://www.work.ua/jobs/8242731/", "<title>Вакансія: Бухгалтер</title>"
        )
        == wl.ALIVE
    )


def test_the_job_removed_redirect_is_the_signal():
    assert (
        wl.classify(
            8346465,
            200,
            "https://www.work.ua/jobs-remote-%D0%BC%D0%B5%D0%BD%D0%B5%D0%B4%D0%B6/?job_removed=1",
            "",
        )
        == wl.REMOVED
    )


def test_the_removal_notice_counts_once_we_were_redirected_away():
    assert (
        wl.classify(8346465, 200, "https://www.work.ua/jobs-remote-x/", REMOVED_BODY)
        == wl.REMOVED
    )


def test_a_back_link_to_a_removed_search_does_not_kill_a_live_posting():
    """The false positive that made the first version of this unusable.

    work.ua puts a "Повернутися до списку" link into every job page carrying the
    URL of the previous search. Probe a removed posting, and the next live page
    quotes `job_removed=1` in its own body — which the first version read as a
    removal and reported two healthy vacancies as gone.
    """
    body = (
        '<a href="/jobs-remote-%D0%BC%D0%B5%D0%BD%D0%B5%D0%B4/?job_removed=1#8374143"'
        ' title="Повернутися до списку">Повернутися</a>'
    )
    assert wl.classify(8374143, 200, "https://www.work.ua/jobs/8374143/", body) == wl.ALIVE


def test_the_removal_notice_in_a_live_page_body_is_ignored_too():
    """Same class of bug: only where we landed decides."""
    assert (
        wl.classify(8374143, 200, "https://www.work.ua/jobs/8374143/", REMOVED_BODY) == wl.ALIVE
    )


def test_a_404_is_a_removal():
    assert (
        wl.classify(999999, 404, "https://www.work.ua/jobs/999999/", "")
        == wl.UNKNOWN_OR_REMOVED_404
    )


def test_a_redirect_somewhere_unexpected_is_not_a_removal():
    """A challenge page or login wall is doubt, and doubt never reports a death."""
    assert wl.classify(8242731, 200, "https://www.work.ua/login", "") == wl.UNKNOWN


def test_work_ua_being_down_is_not_a_removal():
    """The whole point: 5xx and blocks must never fire the alarm."""
    for code in (500, 502, 503, 429, 403):
        assert wl.classify(8242731, code, "https://www.work.ua/jobs/8242731/", "") == wl.UNKNOWN


def test_first_sight_of_a_dead_posting_is_worth_saying():
    changes = wl.transitions({}, {"8346465": wl.REMOVED})
    assert changes == [("8346465", None, wl.REMOVED)]


def test_a_posting_that_dies_is_reported_once_not_every_run():
    previous = {"8346465": wl.ALIVE}
    current = {"8346465": wl.REMOVED}
    assert wl.transitions(previous, current) == [("8346465", wl.ALIVE, wl.REMOVED)]
    # Same state next run — nothing new to say.
    assert wl.transitions(current, current) == []


def test_becoming_unreachable_is_never_reported():
    assert wl.transitions({"8242731": wl.ALIVE}, {"8242731": wl.UNKNOWN}) == []
    assert wl.transitions({}, {"8242731": wl.UNKNOWN}) == []


def test_coming_back_to_life_is_reported():
    assert wl.transitions({"8242731": wl.REMOVED}, {"8242731": wl.ALIVE}) == [
        ("8242731", wl.REMOVED, wl.ALIVE)
    ]


def test_an_unreachable_run_does_not_erase_what_we_knew():
    """Otherwise one bad night resets every posting and re-alerts on all of them."""
    merged = wl.merge_states({"8242731": wl.ALIVE, "8346465": wl.REMOVED}, {"8242731": wl.UNKNOWN})
    assert merged == {"8242731": wl.ALIVE, "8346465": wl.REMOVED}


def test_a_known_state_overwrites_the_old_one():
    merged = wl.merge_states({"8242731": wl.ALIVE}, {"8242731": wl.REMOVED})
    assert merged == {"8242731": wl.REMOVED}


@pytest.mark.parametrize(
    "states,expected",
    [
        ({"1": "removed", "2": "removed"}, True),
        ({"1": "alive", "2": "removed"}, False),
        ({}, False),
    ],
)
def test_a_route_is_starved_only_when_every_posting_is_gone(states, expected):
    assert wl.route_is_starved(states) is expected


class _V:
    def __init__(self, key, ids, calls):
        self.key = key
        self.workua_ids = ids
        self.calls_enabled = calls


def _registry(monkeypatch, **routes):
    monkeypatch.setattr(wl.vacancies, "all_vacancies", lambda: routes)


def test_the_report_says_nothing_while_every_posting_is_up(monkeypatch):
    """A block that prints every day gets read as decoration."""
    _registry(monkeypatch, sales=_V("sales", [1, 2], True))
    assert wl.report_block({"1": wl.ALIVE, "2": wl.ALIVE}) == ""


def test_the_report_says_nothing_before_the_first_check(monkeypatch):
    _registry(monkeypatch, sales=_V("sales", [1], True))
    assert wl.report_block({}) == ""


def test_losing_every_posting_names_what_stopped(monkeypatch):
    _registry(monkeypatch, sales=_V("sales", [1, 2], True))
    block = wl.report_block({"1": wl.REMOVED, "2": wl.REMOVED})
    assert "sales" in block
    assert "обдзвін" in block
    assert "перепублікувати" in block


def test_an_intake_only_vacancy_is_described_as_collection_not_calls(monkeypatch):
    _registry(monkeypatch, accountant=_V("accountant", [7], False))
    block = wl.report_block({"7": wl.REMOVED})
    # The footer names the panel menu ("Збір і обдзвін"), so assert on the line
    # that describes what actually stopped.
    assert "збір з work.ua стоїть" in block
    assert "обдзвін з work.ua стоїть" not in block


def test_losing_some_but_not_all_is_a_softer_line(monkeypatch):
    _registry(monkeypatch, sales=_V("sales", [1, 2], True))
    block = wl.report_block({"1": wl.REMOVED, "2": wl.ALIVE})
    assert "знято 1 з 2" in block
    assert "стоїть" not in block


def test_an_unreachable_posting_is_not_reported_as_removed(monkeypatch):
    """Same rule as the alerts had: doubt never prints as a removal."""
    _registry(monkeypatch, sales=_V("sales", [1, 2], True))
    assert wl.report_block({"1": wl.UNKNOWN, "2": wl.ALIVE}) == ""
