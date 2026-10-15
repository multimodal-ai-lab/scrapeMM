"""Which scheme the proxy's URL is entered with makes no difference: the browser cannot
use an https:// proxy (every page failed with ERR_PROXY_CONNECTION_FAILED), so it always
speaks to an HTTP proxy as http://, and no scheme at all means http://."""

import pytest

from scrapemm.server.proxy import Proxy, validate_url

pytestmark = pytest.mark.server


@pytest.mark.parametrize("entered, stored", [
    ("http://dc.decodo.com:10007", "http://dc.decodo.com:10007"),
    ("https://dc.decodo.com:10007", "https://dc.decodo.com:10007"),
    ("dc.decodo.com:10007", "http://dc.decodo.com:10007"),
    ("HTTPS://dc.decodo.com:10007/", "https://dc.decodo.com:10007"),
    ("socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
])
def test_any_scheme_or_none_is_taken(entered, stored):
    assert validate_url(entered) == stored


def test_not_a_proxy_address_is_refused():
    with pytest.raises(ValueError):
        validate_url("ftp://dc.decodo.com:10007")
    with pytest.raises(ValueError):
        validate_url("dc.decodo.com")  # No port


def test_the_browser_always_gets_an_http_proxy_as_http():
    for url in ("http://dc.decodo.com:10007", "https://dc.decodo.com:10007"):
        settings = Proxy(url=url, username="u", password="p").for_playwright()
        assert settings == {"server": "http://dc.decodo.com:10007", "username": "u", "password": "p"}
    assert Proxy(url="socks5://127.0.0.1:1080").for_playwright() == {"server": "socks5://127.0.0.1:1080"}


def test_the_other_clients_keep_the_scheme_as_entered():
    assert Proxy(url="https://dc.decodo.com:10007", username="u", password="p").with_auth \
        == "https://u:p@dc.decodo.com:10007"
