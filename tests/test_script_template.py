from src.call.script_template import render_system_prompt


def test_renders_with_defaults_uses_kozyr_trans():
    out = render_system_prompt()
    assert "Єва" in out
    assert "Kozyr Trans" in out
    assert "Менеджер з продажу, логіст" in out
    assert "30 до 65 тисяч гривень" in out
    assert "9:00 до 17:00" in out


def test_includes_all_11_steps():
    out = render_system_prompt()
    for marker in [
        "STEP 1",
        "STEP 2",
        "STEP 3",
        "STEP 4",
        "STEP 5",
        "STEP 6",
        "STEP 7",
        "STEP 8",
        "STEP 9",
        "STEP 10",
        "STEP 11",
    ]:
        assert marker in out, f"missing {marker}"


def test_consent_and_recording_phrase_present():
    out = render_system_prompt()
    assert "записується" in out
    assert "consent_denied" in out


def test_age_discrimination_rule_present():
    out = render_system_prompt()
    assert "NEVER mention age" in out
    assert "discrimination" in out


def test_blacklist_regions_present():
    out = render_system_prompt()
    assert "Київ" in out
    assert "Суми" in out
    assert "Запоріжжя" in out
    assert "Херсон" in out
    assert "Донецька" in out


def test_objection_bank_present():
    out = render_system_prompt()
    assert "тепла база" in out
    assert "видаліть мої дані" in out.lower() or "Видаліть" in out
    assert "30 днів" in out


def test_renders_with_overrides():
    out = render_system_prompt(
        agent_name="Марія",
        company_name="ТОВ Тест",
        candidate_name="Іван Петренко",
        candidate_region="Львів",
    )
    assert "Марія" in out
    assert "ТОВ Тест" in out
    assert "Іван Петренко" in out
    assert "Львів" in out


def test_persona_anti_bot_rule():
    out = render_system_prompt()
    assert "NEVER admit to being AI" in out or "NEVER reveal" in out
