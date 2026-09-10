import asyncio
import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import ElementHandle, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from scrapemm.common.exceptions import RetrievalFailed, TargetUnavailableError
from scrapemm.common.retrieval_integration import RetrievalIntegration
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.download.common import HEADERS
from scrapemm.util import to_scraped_content

logger = logging.getLogger("scrapeMM")

# The post-identifying part of a Threads post URL, e.g. "/@leckerundliebe/post/DdFMDaeGPC3"
# or the short form "/t/DdFMDaeGPC3". Any trailing segment (e.g. "/media") is dropped.
POST_PATH_REGEX = re.compile(r"/(?:@[^/]+/post|t)/[A-Za-z0-9_-]+")

# Elements of the embed page that carry no information about the post itself: the
# (36 x 36 px) avatar thumbnail, the like/comment/repost counters, the "Follow" call
# to action, and the verification/action icons.
NOISE_SELECTORS = (".AvatarContainer", ".ActionBarContainer", ".HeaderTrailingActions", "svg")


class Threads(RetrievalIntegration):
    """Retrieves Threads posts via their embed page.

    The regular post page is a login-walled SPA which hides the post's media and
    surrounds it with unrelated posts ("Related threads"). The embed page, in contrast,
    is publicly accessible and contains exactly the requested post along with all of
    its media, so no credentials are needed."""

    name = "Threads"
    domains = ["threads.com", "threads.net"]

    async def _connect(self):
        logger.info("✅ Threads integration ready (embed mode).")
        self.connected = True

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        embed_url = self._to_embed_url(url)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=HEADERS["User-Agent"])
                page = await context.new_page()
                await page.goto(embed_url, timeout=60000, wait_until="domcontentloaded")
                post = await self._wait_for_post(page, embed_url)
                html = await post.evaluate("el => el.outerHTML")
            finally:
                await browser.close()

        # The media URLs are plain, signed CDN links, so they can be downloaded
        # directly (no need to keep the browser around).
        return await to_scraped_content(
            self._remove_noise(html), session=kwargs["session"],
            output_format=kwargs.get("output_format", "multimodal"), url=embed_url
        )

    @staticmethod
    async def _wait_for_post(page: Page, url: str) -> ElementHandle:
        """Waits for the embed page to render the post and returns its container."""
        try:
            post = await page.wait_for_selector(".EmbedContainer", timeout=30000)
        except PlaywrightTimeoutError:
            body = await page.inner_text("body")
            if "not available" in body.lower():
                raise TargetUnavailableError(f"Threads post is not available: {url}")
            raise RetrievalFailed(f"Threads embed page did not render a post: {url}")

        assert post is not None

        # The embed renders text and media in one go. Still, wait until the container
        # stops growing so that any lazily attached media (carousel items, video
        # sources) is part of the returned HTML.
        previous_size = -1
        for _ in range(10):  # up to ~5 s
            size = len(await post.evaluate("el => el.outerHTML"))
            if size == previous_size:
                break
            previous_size = size
            await asyncio.sleep(0.5)

        return post

    @staticmethod
    def _remove_noise(html: str) -> str:
        """Strips the embed's chrome, keeping author, text, date, and media."""
        soup = BeautifulSoup(html, "html.parser")
        for selector in NOISE_SELECTORS:
            for element in soup.select(selector):
                element.decompose()
        return str(soup)

    @staticmethod
    def _to_embed_url(url: str) -> str:
        """Turns a Threads post URL into the URL of the post's embed page."""
        match = POST_PATH_REGEX.match(urlparse(url).path)
        if not match:
            raise NotImplementedError(f"Only Threads posts are supported, got: {url}")
        return f"https://www.threads.com{match.group(0)}/embed"
