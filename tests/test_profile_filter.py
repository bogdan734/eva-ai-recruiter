from datetime import date

from src.match.profile_filter import (
    Gender,
    detect_gender_from_name,
    evaluate,
    extract_birth_year,
)


def _yr(age: int) -> int:
    return date.today().year - age


def test_sales_manager_with_logistics_passes():
    res = evaluate(
        full_name="Олена Петрівна",
        region="Львівська обл.",
        desired_position="Менеджер з продажу",
        last_position="Менеджер з активних продажів",
        birth_year=_yr(30),
    )
    assert res.accepted, res.reason


def test_logist_in_whitelist_passes():
    res = evaluate(
        full_name="Андрій Іванович",
        region="Вінницька обл.",
        desired_position="Логіст",
        last_position="Логіст міжнародних перевезень",
        birth_year=_yr(35),
    )
    assert res.accepted


def test_cashier_rejected():
    res = evaluate(
        full_name="Тетяна Іванівна",
        region="Львівська обл.",
        desired_position="Касир",
        last_position="Касир АТБ",
        birth_year=_yr(28),
    )
    assert not res.accepted
    assert "negative" in res.reason or "no positive" in res.reason


def test_pharmacy_rejected():
    res = evaluate(
        full_name="Олена Петрівна",
        region="Львівська обл.",
        desired_position="Фармацевт",
        last_position="Фармацевт аптеки",
        birth_year=_yr(32),
    )
    assert not res.accepted


def test_beauty_self_employed_rejected():
    res = evaluate(
        full_name="Олена Петрівна",
        region="Львівська обл.",
        desired_position="Майстер манікюру",
        last_position="Майстер манікюру",
        birth_year=_yr(28),
    )
    assert not res.accepted


def test_kyiv_city_rejected():
    res = evaluate(
        full_name="Андрій Іванович",
        region="м.Київ",
        desired_position="Менеджер з продажу",
        last_position="Sales manager",
        birth_year=_yr(30),
    )
    assert not res.accepted
    assert "region" in res.reason


def test_sumy_rejected():
    res = evaluate(
        full_name="Олена Петрівна",
        region="Сумська обл.",
        desired_position="Менеджер з продажу",
        last_position="Sales manager B2B",
        birth_year=_yr(30),
    )
    assert not res.accepted


def test_donetsk_rejected():
    res = evaluate(
        full_name="Андрій Іванович",
        region="Донецька обл.",
        desired_position="Логіст",
        last_position="Логіст",
        birth_year=_yr(35),
    )
    assert not res.accepted


def test_age_over_max_female():
    res = evaluate(
        full_name="Олена Петрівна",
        region="Львівська обл.",
        desired_position="Менеджер з продажу",
        last_position="Sales manager",
        birth_year=_yr(45),
        gender=Gender.FEMALE,
    )
    assert not res.accepted
    assert "age" in res.reason


def test_age_over_max_male():
    res = evaluate(
        full_name="Андрій Іванович",
        region="Львівська обл.",
        desired_position="Логіст",
        last_position="Логіст",
        birth_year=_yr(42),
        gender=Gender.MALE,
    )
    assert not res.accepted


def test_age_too_young():
    res = evaluate(
        full_name="Андрій Іванович",
        region="Львівська обл.",
        desired_position="Логіст",
        last_position="Логіст",
        birth_year=_yr(21),
        gender=Gender.MALE,
    )
    assert not res.accepted


def test_missing_age_does_not_reject():
    res = evaluate(
        full_name="Олена Петрівна",
        region="Львівська обл.",
        desired_position="Менеджер з продажу",
        last_position="Sales manager",
        birth_year=None,
    )
    assert res.accepted


def test_missing_region_does_not_reject():
    res = evaluate(
        full_name="Олена Петрівна",
        region=None,
        desired_position="Менеджер з продажу",
        last_position="Sales manager",
        birth_year=_yr(30),
    )
    assert res.accepted


def test_country_filter():
    res = evaluate(
        full_name="John Smith",
        region="Львівська обл.",
        desired_position="Sales manager",
        last_position="Sales rep",
        birth_year=_yr(30),
        country="US",
    )
    assert not res.accepted


def test_torgovyi_predstavnyk_rejected_when_alone():
    res = evaluate(
        full_name="Андрій Іванович",
        region="Львівська обл.",
        desired_position="Торговий представник",
        last_position="Торговий представник",
        birth_year=_yr(30),
    )
    assert not res.accepted


def test_extract_birth_year_finds_plausible():
    assert extract_birth_year("Народився 1988 року, м. Львів") == 1988


def test_extract_birth_year_returns_none_for_no_match():
    assert extract_birth_year("Жодних дат немає") is None


def test_gender_detection_female():
    assert detect_gender_from_name("Олена Петрівна") == Gender.FEMALE


def test_gender_detection_male():
    assert detect_gender_from_name("Андрій Іванович") == Gender.MALE


def test_gender_unknown():
    assert detect_gender_from_name(None) == Gender.UNKNOWN


# ---------- New rules (2026-06-20 clarifications) ----------


def test_q1_age_22_with_profile_education_passes():
    # 22-year-old female would fail default 23 floor, but profile edu opens 22 window
    res = evaluate(
        full_name="Олена Петрівна",
        region="Львівська обл.",
        desired_position="Менеджер з продажу",
        last_position="Sales manager",
        education_text="Львівський комерційний університет, спеціальність Менеджмент",
        birth_year=_yr(22),
        gender=Gender.FEMALE,
    )
    assert res.accepted, res.reason
    assert res.has_profile_edu


def test_q1_age_22_without_education_rejected():
    res = evaluate(
        full_name="Олена Петрівна",
        region="Львівська обл.",
        desired_position="Менеджер з продажу",
        last_position="Sales manager",
        education_text="ПТУ, кулінарія",
        birth_year=_yr(22),
        gender=Gender.FEMALE,
    )
    assert not res.accepted
    assert "age" in res.reason


def test_q2_logistics_education_recognized():
    from src.match.profile_filter import has_profile_education

    assert has_profile_education("Магістр з логістики")
    assert has_profile_education("Спеціальність — продажі")
    assert has_profile_education("Маркетинг та реклама")
    assert has_profile_education("Менеджмент організацій")
    assert has_profile_education("Економіка підприємства")
    assert not has_profile_education("Кулінарія")
    assert not has_profile_education("Стоматологія")
    assert not has_profile_education(None)


def test_q3_past_self_employed_now_sales_passes():
    # last position is now a sales manager; manicure was in the past
    res = evaluate(
        full_name="Олена Петрівна",
        region="Львівська обл.",
        desired_position="Менеджер з продажу",
        last_position="Менеджер з продажу B2B",  # current role
        experience_text=(
            "01.2024 - тепер: Менеджер з продажу B2B, ТОВ Альфа\n"
            "06.2019 - 12.2023: Майстер манікюру, самозайнята"
        ),
        birth_year=_yr(30),
    )
    assert res.accepted, res.reason


def test_q4_no_manager_role_in_last_3y_rejected():
    # Has positive markers somewhere but last 3y was just a sales rep without
    # manager-grade role, and no recent qualifying record
    res = evaluate(
        full_name="Андрій Іванович",
        region="Львівська обл.",
        desired_position="Менеджер з продажу",  # desire, not actual role
        last_position="Складальник на виробництві",
        experience_text=(
            "01.2023 - тепер: Складальник на виробництві\n"
            "01.2018 - 12.2022: Sales rep B2B"
        ),
        birth_year=_yr(35),
    )
    # The last_position has no negative marker, but recent-role check should reject
    # Note: this is sensitive to whether negative title detection in last_position
    # triggers first. If not, recent-role check catches it.
    assert not res.accepted


def test_q4_war_pause_exception_passes():
    # Last qualifying role ended in 2022 — war-pause exception applies
    res = evaluate(
        full_name="Олена Петрівна",
        region="Львівська обл.",
        desired_position="Менеджер з продажу",
        last_position="Sales manager",
        experience_text=(
            "01.2020 - 02.2022: Менеджер з продажу, ТОВ Альфа\n"
            "06.2018 - 12.2019: Account manager"
        ),
        birth_year=_yr(34),
    )
    assert res.accepted, res.reason
    assert res.war_pause_exception is True or res.recent_manager_role is True


def test_q5_unclear_sales_type_passes_pre_filter():
    # Just "Менеджер з продажу" in last 2 years — no type specified
    # Pre-filter should NOT reject; AI will ask on call
    res = evaluate(
        full_name="Олена Петрівна",
        region="Львівська обл.",
        desired_position="Менеджер з продажу",
        last_position="Менеджер з продажу",
        experience_text="06.2024 - тепер: Менеджер з продажу, ТОВ Альфа",
        birth_year=_yr(28),
    )
    assert res.accepted, res.reason


def test_recent_role_unknown_when_no_experience_text_passes():
    # No experience_text provided → recent_role check returns None (unknown)
    # → don't reject; let AI verify on call
    res = evaluate(
        full_name="Олена Петрівна",
        region="Львівська обл.",
        desired_position="Менеджер з продажу",
        last_position="Менеджер з продажу",
        experience_text=None,
        birth_year=_yr(30),
    )
    assert res.accepted
    assert res.recent_manager_role is None  # explicitly unknown


def test_parse_work_periods_basic():
    from src.match.profile_filter import parse_work_periods

    text = (
        "01.2023 - тепер: Менеджер з продажу, ТОВ Альфа\n"
        "06.2020 - 12.2022: Sales rep, ТОВ Бета"
    )
    periods = parse_work_periods(text)
    assert len(periods) == 2
    years = sorted([p.end_year for p in periods if p.end_year])
    assert 2022 in years


def test_recent_role_check_returns_none_for_empty():
    from src.match.profile_filter import check_recent_role

    ok, diag = check_recent_role(None, recent_years=3, war_pause_year=2022, war_pause_tolerance=1)
    assert ok is None
    assert diag.get("periods_found") == 0
