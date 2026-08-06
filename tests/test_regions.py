from src.common.regions import is_region_allowed, normalize_region


def test_normalize_known_aliases():
    assert normalize_region("Київська обл.") == "Київська"
    assert normalize_region("м.Київ") == "м. Київ"
    assert normalize_region("Kyiv") == "м. Київ"


def test_normalize_unknown_passthrough():
    """Anything we have no mapping for is returned as-is (and then fails the
    whitelist, which is the safe direction). Note «Львів» is NOT an example of
    this any more — known cities now resolve to their oblast."""
    assert normalize_region("Смородське") == "Смородське"
    assert normalize_region("Berlin") == "Berlin"


def test_cities_map_to_their_oblast():
    """Єва asks for a CITY; the whitelist is written in OBLASTS. Before this,
    «Львів» and «Одеса» were rejected while Львівська and Одеська were allowed."""
    assert normalize_region("Дніпро") == "Дніпропетровська"
    assert normalize_region("Кривий Ріг") == "Дніпропетровська"
    assert normalize_region("Львів") == "Львівська"
    assert normalize_region("Одеса") == "Одеська"
    assert normalize_region("Луцьк") == "Волинська"
    assert normalize_region("Ужгород") == "Закарпатська"


def test_city_passes_the_filter_its_oblast_passes():
    allowed = {"Львівська", "Одеська", "Дніпропетровська"}
    blocked = {"Вінницька"}
    for city in ("Львів", "Одеса", "Дніпро", "Кривий Ріг"):
        assert is_region_allowed(normalize_region(city), allowed, blocked), city
    assert not is_region_allowed(normalize_region("Вінниця"), allowed, blocked)


def test_region_filter():
    allowed = {"Київська", "Львівська", "Вінницька"}
    blocked = {"м. Київ"}
    assert is_region_allowed("Київська обл.", allowed, blocked)
    assert not is_region_allowed("м. Київ", allowed, blocked)
    assert not is_region_allowed("Одеська", allowed, blocked)
