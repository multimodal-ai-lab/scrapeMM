"""Archive.today's refusals ("Too Many Requests") are no CAPTCHA: its other routes are
tried, and where it refuses them all, the URLs wait and it is asked again later."""

import pytest

from scrapemm.server.integrations import archive_today as at
from scrapemm.server.proxy import Proxy

pytestmark = pytest.mark.server

PROXIES = [Proxy(url="http://isp.example:10001"), Proxy(url="http://isp.example:10002")]


@pytest.fixture
def routes(monkeypatch):
    """Routes answering as given ({proxy url or None: state}); the route chosen is recorded."""
    answers, config = {}, {}
    from scrapemm.server import proxy
    monkeypatch.setattr(proxy, "configured", lambda: PROXIES)
    monkeypatch.setattr(proxy, "usable", lambda p, *a, **k: True)
    monkeypatch.setattr(at, "update_config", lambda **kw: config.update(kw))
    monkeypatch.setattr(at, "get_config_var", lambda name, default=None: config.get(name, default))
    monkeypatch.setattr(at, "_refused_until", 0.0)
    monkeypatch.setattr(at, "_last_reroute", (0.0, False))
    monkeypatch.setattr(at, "_ensure_watcher", lambda: None)

    async def state(via):
        return answers[via.url if via is not None else None]
    monkeypatch.setattr(at, "_route_state", state)
    return answers, config


def test_the_refusal_is_told_from_the_check():
    assert at._is_refusal("<html><body><h1>Too Many Requests</h1></body></html>", 429)
    assert not at._is_refusal("<title>Security check</title> ... recaptcha ...", 429)
    assert not at._is_refusal("Too Many Requests", 200)


async def test_the_first_route_that_answers_becomes_the_route(routes):
    answers, config = routes
    answers.update({None: at.REFUSED, PROXIES[0].url: at.REFUSED, PROXIES[1].url: at.GATE})
    assert await at._reroute()
    assert config[at.ROUTE_KEY] == PROXIES[1].url
    assert at._route().url == PROXIES[1].url and not at._refusing()


async def test_refused_everywhere_the_urls_wait(routes):
    answers, config = routes
    answers.update({None: at.REFUSED, PROXIES[0].url: at.REFUSED, PROXIES[1].url: at.REFUSED})
    assert not await at._reroute()
    assert at._refusing() and at.ROUTE_KEY not in config
    assert not await at._reroute()  # Not asked again before the pause is over


async def test_back_to_this_servers_address_when_it_answers_again(routes):
    answers, config = routes
    config[at.ROUTE_KEY] = PROXIES[0].url
    answers.update({None: at.CONTENT, PROXIES[0].url: at.REFUSED, PROXIES[1].url: at.GATE})
    assert await at._reroute()
    assert config[at.ROUTE_KEY] is None and at._route() is None
