from src.common.phone import is_ukrainian, normalize_phone


def test_normalize_ua_local():
    assert normalize_phone("0671234567") == "+380671234567"


def test_normalize_ua_international():
    assert normalize_phone("+380 67 123 45 67") == "+380671234567"


def test_normalize_ua_with_parens():
    assert normalize_phone("(067) 123-45-67") == "+380671234567"


def test_invalid_returns_none():
    assert normalize_phone("abc") is None
    assert normalize_phone("") is None
    assert normalize_phone("123") is None


def test_is_ukrainian():
    assert is_ukrainian("+380671234567")
    assert not is_ukrainian("+15555555555")
