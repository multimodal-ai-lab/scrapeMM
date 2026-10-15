"""Several proxies: which one a method goes through, by their tests and their record with
that method -- refused ones rest, failing ones drop out, the rest take turns."""

import time

import pytest

from scrapemm.common.exceptions import (AccessBlockedError, CaptchaEncounteredError,
                                        RegionBlockedError, RetrievalFailed)
from scrapemm.server import config, proxy

pytestmark = pytest.mark.server

A, B, C = "http://dc.decodo.com:10001", "http://dc.decodo.com:10002", "http://dc.decodo.com:10003"


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    values = {proxy.CONFIG_KEY: [{"url": u, "enabled": True} for u in (A, B, C)]}
    monkeypatch.setattr(config, "_config", values)
    monkeypatch.setattr(proxy, "_secret", lambda name: None)
    monkeypatch.setattr(proxy, "_health", proxy._HealthStore(tmp_path / "proxy_health.json"))
    monkeypatch.setattr(proxy, "_refusal_listeners", [])
    monkeypatch.setattr(proxy, "_in_flight", {})
    return values


def refusal():
    return AccessBlockedError("The site refused to serve this address: HTTP 403 Forbidden")


def test_all_endpoints_are_configured_in_their_order():
    assert [p.url for p in proxy.configured()] == [A, B, C]


def test_they_take_turns_while_equally_good():
    picked = []
    for _ in range(3):
        p = proxy.pick("browser")
        picked.append(p.url)
        proxy.note_use(p)
        time.sleep(0.01)
    assert sorted(picked) == [A, B, C]


def test_parallel_attempts_spread_across_idle_endpoints():
    """YouTube in parallel: each retrieval through an endpoint of its own."""
    for _ in range(5):
        proxy.learn(proxy.find(A), "YouTube", None)  # The best, but busy
    picked = []
    with proxy.attempt(proxy.pick("YouTube")):
        picked.append(proxy.route().url)
        with proxy.attempt(proxy.pick("YouTube")):
            picked.append(proxy.route().url)
            assert proxy.idle("YouTube")
            with proxy.attempt(proxy.pick("YouTube")):
                picked.append(proxy.route().url)
                assert not proxy.idle("YouTube")
    assert picked[0] == A and sorted(picked) == [A, B, C]
    assert proxy._in_flight == {A: 0, B: 0, C: 0}
    assert proxy.usable_count("YouTube") == 3


def test_a_refused_endpoint_rests_from_that_platform_only():
    proxy.learn(proxy.find(A), "TikTok", refusal())
    proxy.learn(proxy.find(B), "TikTok", refusal())
    assert proxy.pick("TikTok").url == C
    assert proxy.pick("TikTok", exclude=[C]) is None
    assert proxy.available("Instagram")  # Another method may still go through them


def test_a_website_refusal_rests_it_from_that_website_only():
    proxy.learn(proxy.find(A), "browser", refusal(), site="example.com")
    assert not proxy.usable(proxy.find(A), "browser", site="example.com")
    assert proxy.usable(proxy.find(A), "browser", site="other.org")


def _located(countries):
    for url, country in zip((A, B, C), countries):
        proxy._health.get(url).test = {"ok": True, "country": country}


def test_a_region_block_moves_on_to_another_region_and_mars_nothing():
    _located(["DE", "FR", "US"])
    blocked = RegionBlockedError("https://news.example is blocked in this server's region (HTTP 451).")
    proxy.learn(proxy.find(A), "browser", blocked, site="news.example")
    assert proxy._health.peek(A).methods == {}  # No reputation lost
    avoid = {proxy.region_of(proxy.find(A))}
    assert avoid == {"Europe"}
    assert proxy.pick("browser", site="news.example", avoid_regions=avoid).url == C
    assert proxy.usable(proxy.find(A), "browser", site="elsewhere.example")


def test_the_better_reputation_wins():
    for _ in range(5):
        proxy.learn(proxy.find(B), "YouTube", None)
    proxy.note_use(proxy.find(B))  # Used last: would lose a tie
    assert proxy.pick("YouTube").url == B


def test_a_failed_test_or_a_failing_proxy_drops_it():
    proxy._health.get(A).test = {"ok": False, "error": "timeout"}
    proxy.learn(proxy.find(B), "browser",
                RetrievalFailed("net::ERR_PROXY_CONNECTION_FAILED at https://example.com"))
    assert proxy.pick("browser").url == C
    assert proxy.status()["usable_count"] == 1


def test_a_site_error_says_nothing_about_the_proxy():
    proxy.learn(proxy.find(A), "browser", RetrievalFailed("The page is empty."))
    assert proxy.usable(proxy.find(A), "browser")
    assert proxy._health.peek(A).methods == {}


def test_a_captcha_mars_the_reputation_without_a_rest():
    proxy.learn(proxy.find(A), "browser", CaptchaEncounteredError("A CAPTCHA blocks the page."))
    record = proxy._health.peek(A).methods["browser"]
    assert record.refused == 1 and record.rest_until == 0
    assert proxy.pick("browser").url != A


def test_youtube_rests_as_long_as_its_pause(setup):
    setup["youtube_pause"] = 600
    proxy.learn(proxy.find(A), "YouTube", RetrievalFailed("Sign in to confirm you're not a bot"))
    rest = proxy._health.peek(A).methods["YouTube"].rest_until - time.time()
    assert 590 < rest <= 600


def test_refusals_are_announced():
    told = []
    proxy.on_refusal(told.append)
    proxy.learn(proxy.find(A), "YouTube", RetrievalFailed("Sign in to confirm you're not a bot"))
    assert told == ["YouTube"]


def test_the_records_outlast_a_restart(tmp_path):
    proxy._health.get(A).test = {"ok": True, "ip": "1.2.3.4", "country": "DE", "city": "Berlin"}
    proxy.learn(proxy.find(A), "browser", refusal())
    reloaded = proxy._HealthStore(proxy._health.path)
    assert reloaded.peek(A).test["city"] == "Berlin"
    assert reloaded.peek(A).methods["browser"].refused == 1


def test_old_outcomes_count_less():
    record = proxy.Record(ok=0, refused=4, at=time.time() - 30 * 86400)
    assert record.score(time.time()) > 0.45  # A month on, the refusals are all but forgotten


def test_saving_drops_the_records_of_removed_endpoints(monkeypatch):
    monkeypatch.setattr(proxy, "update_config", lambda **kw: config._config.update(kw))
    monkeypatch.setattr(proxy, "remove_secret", lambda name: None)
    proxy._health.get(C).test = {"ok": True}
    proxy.save([{"url": A}, {"url": "dc.decodo.com:10002"}])
    assert [p.url for p in proxy.configured()] == [A, B]
    assert C not in proxy._health.entries
    with pytest.raises(ValueError):
        proxy.save([{"url": A}, {"url": "dc.decodo.com:10001"}])  # The same twice
