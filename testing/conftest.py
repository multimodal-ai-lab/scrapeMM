"""Shared test setup.

The suite is split in two. Tests that need only the client package (the wire contract,
the media-transfer decision) run anywhere `pip install scrapeMM` works. Tests marked
`server` need the scraping stack from requirements-server.txt and are skipped without
it, so that a client-only checkout still has a green suite rather than a wall of
import errors.
"""

import pytest


def pytest_collection_modifyitems(config, items):
    """Skips the server-marked tests when the server dependencies are absent."""
    try:
        import playwright  # noqa: F401
        available = True
    except ModuleNotFoundError:
        available = False

    if available:
        return

    skip = pytest.mark.skip(reason="needs the server dependencies "
                                   "(pip install -r requirements-server.txt)")
    for item in items:
        if "server" in item.keywords:
            item.add_marker(skip)
