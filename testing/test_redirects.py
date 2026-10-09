"""A host that redirects to a host that does not exist: the URL is repaired if the redirect
only lost a slash, and else the host counts as dead, rather than every method trying it."""

import pytest

from scrapemm.server import reachability

pytestmark = pytest.mark.server

# What www.malayalam.factcrescendo.com answered every URL with: the slash after the host
# lost, so that the path became part of a host name
URL = "https://www.malayalam.factcrescendo.com/old-image-of-gujarat-flood-shared-as-recent/"
SENT_TO = "https://malayalam.factcrescendo.comold-image-of-gujarat-flood-shared-as-recent/"


class FakeResponse:
    def __init__(self, status, location=None):
        self.status = status
        self.headers = {"Location": location} if location else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, status, location=None):
        self.response, self.requests = FakeResponse(status, location), []

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False  # What it looks at is the redirect itself
        self.requests.append(url)
        return self.response


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(reachability, "_redirects", {})
    monkeypatch.setattr(reachability, "_verdicts", {})
    monkeypatch.setattr(reachability, "_behind_proxy", lambda: False)
    existing = {"www.malayalam.factcrescendo.com", "malayalam.factcrescendo.com", "example.org"}

    async def resolves(host):
        return host in existing

    monkeypatch.setattr(reachability, "_resolves", resolves)


async def test_a_redirect_that_lost_its_slash_is_repaired():
    session = FakeSession(301, SENT_TO)
    repaired = await reachability.redirect_rewrite(URL, session)
    assert repaired == "https://malayalam.factcrescendo.com/old-image-of-gujarat-flood-shared-as-recent/"


async def test_the_whole_host_is_repaired_after_one_request():
    session = FakeSession(301, SENT_TO)
    await reachability.redirect_rewrite(URL, session)
    other = "https://www.malayalam.factcrescendo.com/another-article/"
    assert await reachability.redirect_rewrite(other, session) == \
        "https://malayalam.factcrescendo.com/another-article/"
    assert len(session.requests) == 1


async def test_a_redirect_to_a_host_that_does_not_exist_makes_the_host_dead():
    session = FakeSession(301, "https://nowhere.invalid/page")
    assert await reachability.redirect_rewrite("https://example.org/page", session) is None
    verdict, reason = await reachability.check("https://example.org/page")
    assert verdict == "dead" and "nowhere.invalid" in reason


async def test_a_redirect_to_a_host_that_exists_is_left_alone():
    session = FakeSession(301, "https://example.org/elsewhere")
    assert await reachability.redirect_rewrite("https://www.malayalam.factcrescendo.com/x", session) is None
    verdict, _ = await reachability.check("https://www.malayalam.factcrescendo.com/x")
    assert verdict != "dead"


async def test_a_page_that_does_not_redirect_is_left_alone():
    session = FakeSession(200)
    assert await reachability.redirect_rewrite("https://example.org/page", session) is None


async def test_a_failed_probe_gives_no_verdict():
    class Failing:
        def get(self, *args, **kwargs):
            raise OSError("no route")

    assert await reachability.redirect_rewrite("https://example.org/page", Failing()) is None
    assert (await reachability.check("https://example.org/page"))[0] != "dead"


def test_only_the_slash_lost_by_a_redirect_is_repaired():
    meant = reachability._meant_host
    assert meant("www.malayalam.factcrescendo.com", "malayalam.factcrescendo.comold-image") == \
        "malayalam.factcrescendo.com"
    assert meant("example.org", "example.orgslug") == "example.org"
    # A host that merely starts alike is another host
    assert meant("example.org", "example.org.evil.net") is None
    assert meant("example.org", "example.org") is None
    assert meant("example.org", "other.example") is None
