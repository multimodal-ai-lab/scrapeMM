"""A server error page is no content: neither a live site's nor an archive's (e.g.
Ghostarchive behind Cloudflare answering "error code: 522" while its origin is down).
Exercised with the real browser against a local HTTP server."""

import pytest
from aiohttp import web

from scrapemm.common.exceptions import RetrievalFailed
from scrapemm.server.integrations import browser
from scrapemm.server.integrations.ghostarchive import Ghostarchive

pytestmark = pytest.mark.server


@pytest.fixture
async def site():
    async def error(request):
        status = int(request.match_info["status"])
        return web.Response(status=status, text=f"error code: {status}")

    async def page(request):
        return web.Response(content_type="text/html", text="<html><body><main><p>"
                            + "An actual article. " * 40 + "</p></main></body></html>")

    app = web.Application()
    app.router.add_get("/status/{status}", error)
    app.router.add_get("/page", page)
    runner = web.AppRunner(app)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest.mark.parametrize("status", [500, 502, 522])
async def test_live_server_error_is_no_content(site, status):
    with pytest.raises(RetrievalFailed, match=f"HTTP {status}"):
        await browser._get(f"{site}/status/{status}", output_format="html")


async def test_archive_down_is_no_content(site):
    with pytest.raises(RetrievalFailed, match="Ghostarchive is unavailable right now"):
        await Ghostarchive()._get(f"{site}/status/522", output_format="html")


async def test_a_page_still_is_content(site):
    content = await browser._get(f"{site}/page", output_format="html")
    assert "An actual article." in content.html
