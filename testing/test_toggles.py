"""Switching retrieval methods off."""

import pytest

pytest.importorskip("playwright", reason="needs the server dependencies")

from scrapemm.server import toggles  # noqa: E402

pytestmark = pytest.mark.server


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    """Keeps the toggles in memory, so a test run cannot rewrite the real config."""
    store: dict = {}
    monkeypatch.setattr(toggles, "get_config_var",
                        lambda name, default=None: store.get(name, default))
    monkeypatch.setattr(toggles, "update_config", lambda **kwargs: store.update(kwargs))
    return store


def test_everything_is_enabled_by_default():
    assert toggles.disabled() == set()
    assert toggles.is_enabled("decodo")


def test_disabling_and_re_enabling_round_trips():
    toggles.set_enabled("Decodo", False)
    assert not toggles.is_enabled("decodo")
    toggles.set_enabled("Decodo", True)
    assert toggles.is_enabled("decodo")


def test_names_are_matched_case_insensitively():
    """The API takes whatever the UI sends; 'X (Twitter)' and 'x (twitter)' are one
    and the same method."""
    toggles.set_enabled("X (Twitter)", False)
    assert not toggles.is_enabled("x (twitter)")
    assert not toggles.is_enabled("X (TWITTER)")


def test_filter_methods_drops_disabled_ones_and_keeps_order():
    toggles.set_enabled("firecrawl", False)
    assert toggles.filter_methods(["decodo", "firecrawl", "Threads"]) == ["decodo", "Threads"]


def test_disabling_twice_does_not_duplicate_the_entry(isolated_config):
    toggles.set_enabled("decodo", False)
    toggles.set_enabled("decodo", False)
    assert isolated_config[toggles.CONFIG_KEY] == ["decodo"]


def test_enabling_something_never_disabled_is_harmless():
    toggles.set_enabled("bluesky", True)
    assert toggles.is_enabled("bluesky")
