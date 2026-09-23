"""The dashboard's status probes."""

import pytest

pytest.importorskip("playwright", reason="needs the server dependencies")

from scrapemm.server import status  # noqa: E402

pytestmark = pytest.mark.server


def test_required_secrets_name_real_methods():
    """Guards a bug that is invisible at runtime: a key that matches nothing means the
    dashboard reports 'Could not connect' where it should have said 'you have not
    configured this yet'."""
    from scrapemm.server.integrations import NAME_TO_INTEGRATION

    known = set(NAME_TO_INTEGRATION) | set(status.METHOD_KEYS)
    unknown = set(status.REQUIRED_SECRETS) - known
    assert not unknown, (
        f"REQUIRED_SECRETS keys that match no integration or method: {sorted(unknown)}. "
        f"Keys are lowercase names, e.g. 'x (twitter)'.")


def test_every_method_gets_a_card():
    """Firecrawl and Decodo are not integrations, but the dashboard has to show them --
    Decodo needs a secret, and a dashboard that never mentioned it would leave that
    quietly unexplained."""
    keys = status.all_keys()
    assert "firecrawl" in keys
    assert "decodo" in keys
    assert "archive.today" in keys


def test_required_secrets_name_real_secrets():
    from scrapemm.server.secrets import SECRETS

    for integration, needed in status.REQUIRED_SECRETS.items():
        unknown = set(needed) - set(SECRETS)
        assert not unknown, f"{integration} requires unknown secret(s) {sorted(unknown)}"


async def test_probe_reports_missing_secrets_without_connecting(monkeypatch):
    """An unconfigured integration must be explained, not merely marked broken -- and
    its `_connect()` must not even be attempted."""
    from scrapemm.server.integrations import NAME_TO_INTEGRATION

    integration = NAME_TO_INTEGRATION["x (twitter)"]
    monkeypatch.setattr("scrapemm.server.status.is_set", lambda name: False)

    async def fail():
        raise AssertionError("_connect() must not run when secrets are missing")

    monkeypatch.setattr(integration, "probe", fail)

    result = await status._probe("x (twitter)", integration)
    assert result.connected is False
    assert result.configured is False
    assert result.state == status.UNCONFIGURED
    # The names go to the UI as chips, so they belong in the field, not the sentence
    assert "x_bearer_token" in result.missing_secrets


async def test_probe_survives_a_cancelling_integration(monkeypatch):
    """One integration cancelling its own probe must not fail the dashboard request
    that asked about all of them."""
    import asyncio

    from scrapemm.server.integrations import NAME_TO_INTEGRATION

    integration = NAME_TO_INTEGRATION["youtube"]

    async def cancel():
        raise asyncio.CancelledError()

    monkeypatch.setattr(integration, "probe", cancel)
    result = await status._probe("youtube", integration)
    assert result.connected is False
    assert "Cancelled" in result.detail


async def test_probe_gives_up_on_a_hanging_integration(monkeypatch):
    import asyncio

    from scrapemm.server.integrations import NAME_TO_INTEGRATION

    integration = NAME_TO_INTEGRATION["youtube"]
    monkeypatch.setattr(status, "PROBE_TIMEOUT", 0.05)

    async def hang():
        await asyncio.sleep(10)

    monkeypatch.setattr(integration, "probe", hang)
    result = await status._probe("youtube", integration)
    assert result.connected is False
    assert "within" in result.detail


async def test_headed_browser_probe_does_not_start_a_browser(monkeypatch):
    """A dashboard poll must never launch the shared browser: it would fight the
    CAPTCHA panel and the running retrievals over the profile lock."""
    from scrapemm.server.integrations import NAME_TO_INTEGRATION

    integration = NAME_TO_INTEGRATION["headed browser"]

    async def fail(*args, **kwargs):
        raise AssertionError("the probe must not start the shared browser")

    monkeypatch.setattr(integration, "_ensure_browser", fail)
    monkeypatch.setattr(integration, "_connect", fail)

    await integration.probe()  # Raises only if it tried to start the browser


def test_secrets_map_back_to_methods():
    assert "instagram" in status.secrets_to_integrations("instagram_cookie")
    assert "decodo" in status.secrets_to_integrations("decodo_token")
    assert status.secrets_to_integrations("not_a_secret") == []


async def test_a_disabled_method_reads_as_disabled_not_broken(monkeypatch):
    """A method nobody is going to call should not be reported as a problem."""
    from scrapemm.server.integrations import NAME_TO_INTEGRATION

    integration = NAME_TO_INTEGRATION["youtube"]
    monkeypatch.setattr("scrapemm.server.status.is_enabled", lambda name: False)

    async def fail():
        raise AssertionError("a disabled method must not be probed")

    monkeypatch.setattr(integration, "probe", fail)

    result = await status._probe("youtube", integration)
    assert result.state == status.DISABLED
    assert result.enabled is False


async def test_disabled_wins_over_missing_secrets(monkeypatch):
    """Otherwise a switched-off integration would nag for credentials it will not use."""
    from scrapemm.server.integrations import NAME_TO_INTEGRATION

    monkeypatch.setattr("scrapemm.server.status.is_set", lambda name: False)
    monkeypatch.setattr("scrapemm.server.status.is_enabled", lambda name: False)

    result = await status._probe("instagram", NAME_TO_INTEGRATION["instagram"])
    assert result.state == status.DISABLED


def test_invalidate_clears_cached_statuses():
    status._cache["youtube"] = (0.0, status.IntegrationStatus(name="YouTube"))
    status.invalidate("YouTube")
    assert "youtube" not in status._cache


async def test_optional_secrets_make_an_integration_limited_not_broken(monkeypatch):
    """Facebook and Instagram serve most public content without a cookie, so a missing
    one narrows what they reach rather than stopping them."""
    from scrapemm.server.integrations import NAME_TO_INTEGRATION

    integration = NAME_TO_INTEGRATION["instagram"]
    monkeypatch.setattr("scrapemm.server.status.is_set", lambda name: False)

    async def connect():
        integration.connected = True

    monkeypatch.setattr(integration, "probe", connect)

    result = await status._probe("instagram", integration)
    assert result.state == status.LIMITED
    assert result.configured is True, "no *required* secret is missing"
    assert "instagram_cookie" in result.missing_optional_secrets
    assert result.missing_secrets == []


async def test_a_captcha_gate_is_reported_as_gated_not_as_a_fault(monkeypatch):
    """Archive.today is gated most of the time; reporting that as an outage would have
    the dashboard cry wolf on a perfectly healthy integration."""
    from scrapemm.common.exceptions import CaptchaEncounteredError
    from scrapemm.server.integrations import NAME_TO_INTEGRATION

    integration = NAME_TO_INTEGRATION["archive.today"]

    async def gated():
        raise CaptchaEncounteredError("Gated by the CAPTCHA right now.")

    monkeypatch.setattr(integration, "probe", gated)

    result = await status._probe("archive.today", integration)
    assert result.state == status.GATED
    assert result.gated is True
    assert "CAPTCHA" in result.detail
