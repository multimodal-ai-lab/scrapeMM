import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

import aiohttp
from playwright.async_api import TimeoutError, Page

from scrapemm.common.scraping_response import ScrapedContent, utc_iso
from scrapemm.server.download.common import HEADERS
from scrapemm.server.integrations.headed_browser import HeadedBrowser, ContentTarget

logger = logging.getLogger("scrapeMM")

ITEM_URL = re.compile(r"awesomescreenshot\.com/(image|video)/(\d+)")
# The public endpoints the page loads its item from, upload time included
ITEM_API = {"image": "https://www.awesomescreenshot.com/api/v1/image/load?id={id}&key={key}",
            "video": "https://www.awesomescreenshot.com/api/v1/video/load_video?id={id}&key={key}"}


class AwesomeScreenshot(HeadedBrowser):
    name = "AwesomeScreenshot"
    domains = ["awesomescreenshot.com"]

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        """The image or video, with the time it was uploaded."""
        content = await super()._get(url, **kwargs)
        if content is not None and (uploaded := await _uploaded_at(url)):
            content.published_at = utc_iso(uploaded)
            content = content.prefixed(f"Uploaded to AwesomeScreenshot: "
                                       f"{utc_iso(uploaded)[:16].replace('T', ' ')} UTC\n\n")
        return content

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        """The platform shows either an image or a video. Wait for the matching wrapper."""
        try:
            element = await page.wait_for_selector(
                "div.img-wrapper, div.video-box", timeout=5000
            )
            if element:
                # Prefer an iframe's content frame when present; otherwise the wrapper div.
                if frame := await element.content_frame():
                    return frame
                return element
        except TimeoutError:
            logger.debug("Timed out waiting for AwesomeScreenshot media wrapper.")
        logger.debug("Could not find image or video in AwesomeScreenshot page. "
                     "Perhaps the structure has changed?")
        return page


async def _uploaded_at(url: str) -> Optional[int]:
    """When the item was uploaded (Unix seconds), from the endpoint the page loads it
    from; None if that cannot be told."""
    match = ITEM_URL.search(url)
    key = parse_qs(urlparse(url).query).get("key", [None])[0]
    if not (match and key):
        return None
    kind, item_id = match.groups()
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(ITEM_API[kind].format(id=item_id, key=key),
                                   timeout=aiohttp.ClientTimeout(total=15)) as response:
                data = await response.json(content_type=None)
        created = data["data"][kind]["createdSec"]
        return int(created) if created else None
    except Exception as e:
        logger.debug(f"No upload time for {url}: {type(e).__name__}: {e}")
        return None
