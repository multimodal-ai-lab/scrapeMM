"""Tests the Firecrawl engine's handling of options, with a stand-in for Firecrawl."""

from types import SimpleNamespace

import pytest

from scrapemm.server.integrations.firecrawl.firecrawl import Firecrawl

pytestmark = pytest.mark.server

PAGE = "<html><body><nav>Home</nav><main><p>The article text.</p></main></body></html>"


class FakeClient:
    """Answers every scrape with the same page and remembers the options asked for."""

    def __init__(self):
        self.options = None

    async def scrape(self, url, **options):
        self.options = options
        return SimpleNamespace(html=PAGE, metadata=SimpleNamespace(status_code=200))


@pytest.mark.asyncio
async def test_only_main_content_is_stripped_by_scrapemm():
    """Firecrawl gets the whole page, as every other method does, and scrapeMM strips it."""
    client = FakeClient()
    firecrawl = Firecrawl()
    firecrawl.firecrawl_urls, firecrawl._clients = ["http://firecrawl"], [client]

    content = await firecrawl.scrape("https://example.com", session=None,
                                     output_format="markdown", only_main_content=True)

    assert client.options["only_main_content"] is False
    assert content.html == PAGE
    assert "Home" not in content.markdown
    assert "The article text." in content.markdown
