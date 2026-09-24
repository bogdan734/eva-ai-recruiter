"""Whatever shape the proxy provider gives us has to just work.

Credentials embedded in the server URL are ignored by Chromium, so a pasted
`http://user:pass@host:port` used to mean "connect with no auth" -- the proxy
would refuse and it would look like the proxy itself was broken.
"""
from src.scraper.workua import _proxy_config


def test_empty_means_no_proxy():
    assert _proxy_config(None) is None
    assert _proxy_config("") is None
    assert _proxy_config("   ") is None


def test_plain_url_passes_through():
    assert _proxy_config("http://1.2.3.4:8000") == {"server": "http://1.2.3.4:8000"}


def test_bare_host_port_is_treated_as_http():
    assert _proxy_config("1.2.3.4:8000") == {"server": "http://1.2.3.4:8000"}


def test_credentials_are_split_out_not_left_in_the_server():
    cfg = _proxy_config("http://user:pa55@gate.provider.com:7000")
    assert cfg == {
        "server": "http://gate.provider.com:7000",
        "username": "user",
        "password": "pa55",
    }


def test_socks5_with_credentials():
    cfg = _proxy_config("socks5://u:p@10.0.0.1:1080")
    assert cfg["server"] == "socks5://10.0.0.1:1080"
    assert cfg["username"] == "u"
    assert cfg["password"] == "p"


def test_surrounding_whitespace_is_forgiven():
    assert _proxy_config("  http://1.2.3.4:8000  ") == {"server": "http://1.2.3.4:8000"}
