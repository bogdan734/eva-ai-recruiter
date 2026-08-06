from src.common import vacancies


def test_workua_routing():
    assert vacancies.for_workua(8249916) is vacancies.SALES
    assert vacancies.for_workua(8242731) is vacancies.ACCOUNTANT
    assert vacancies.for_workua("8242731") is vacancies.ACCOUNTANT
    assert vacancies.for_workua(999) is None
    assert vacancies.for_workua(None) is None


def test_robotaua_routing():
    assert vacancies.for_robotaua(11277559) is vacancies.SALES
    assert vacancies.for_robotaua(11284462) is vacancies.SALES
    assert vacancies.for_robotaua(11249166) is vacancies.ACCOUNTANT
    assert vacancies.for_robotaua(11292426) is vacancies.ACCOUNTANT


def test_both_accountant_postings_route_to_one_funnel():
    """Client 05.08: «єдиний» and «помічник» share the Бухгалтер funnel."""
    for wid in (8242731, 8374143):
        assert vacancies.for_workua(wid).keycrm_pipeline_id == 6
    for rid in (11249166, 11292426):
        assert vacancies.for_robotaua(rid).keycrm_pipeline_id == 6


def test_accountant_intake_is_enabled():
    """Back on 05.08: the other integration files accountants under sales, so
    with us out of funnel 6 nobody was filling it."""
    assert vacancies.ACCOUNTANT.intake_enabled
    assert not vacancies.intake_blocked(vacancies.ACCOUNTANT)
    for wid in (8242731, 8374143):
        assert wid in vacancies.workua_ids()
    for rid in (11249166, 11292426):
        assert rid in vacancies.robotaua_ids()


def test_accountant_still_never_gets_called():
    """Enabling intake must not put accountants into the dialer or Єва's chat."""
    assert not vacancies.ACCOUNTANT.calls_enabled
    assert not vacancies.ACCOUNTANT.screen_enabled
    assert not vacancies.ACCOUNTANT.open_paid_contacts
    for rid in (11249166, 11292426):
        assert rid not in vacancies.robotaua_ids(calls_only=True)


def test_unknown_key_falls_back_to_sales():
    assert vacancies.get(None) is vacancies.SALES
    assert vacancies.get("nope") is vacancies.SALES
    assert vacancies.get("accountant") is vacancies.ACCOUNTANT


def test_accountant_is_intake_only():
    a = vacancies.ACCOUNTANT
    assert not a.calls_enabled       # Єва не дзвонить
    assert not a.screen_enabled      # гео/портрет менеджера не застосовуються
    assert not a.open_paid_contacts  # квота robota.ua не витрачається
    assert a.keycrm_pipeline_id == 6
    assert a.keycrm_status_id == 84


def test_calls_only_view_excludes_accountant():
    """The chat poller pulls this view — Єва must not see accountant threads."""
    assert 11249166 not in vacancies.robotaua_ids(calls_only=True)
    assert 11277559 in vacancies.robotaua_ids(calls_only=True)
    assert 8242731 not in vacancies.workua_ids(calls_only=True)
    assert 8249916 in vacancies.workua_ids(calls_only=True)


def test_no_vacancy_id_is_claimed_twice():
    """A board id belonging to two routes would make the funnel non-deterministic."""
    for attr in ("workua_ids", "robotaua_ids"):
        seen: set[int] = set()
        for v in vacancies.VACANCIES.values():
            ids = getattr(v, attr)
            assert not (ids & seen), f"{attr} overlap in {v.key}"
            seen |= ids
