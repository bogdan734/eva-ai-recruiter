from src.common.regions import is_region_allowed, normalize_region


def test_normalize_known_aliases():
    assert normalize_region("Київська обл.") == "Київська"
    assert normalize_region("м.Київ") == "м. Київ"
    assert normalize_region("Kyiv") == "м. Київ"


def test_normalize_unknown_passthrough():
    assert normalize_region("Львів") == "Львів"


def test_region_filter():
    allowed = {"Київська", "Львівська", "Вінницька"}
    blocked = {"м. Київ"}
    assert is_region_allowed("Київська обл.", allowed, blocked)
    assert not is_region_allowed("м. Київ", allowed, blocked)
    assert not is_region_allowed("Одеська", allowed, blocked)
