"""Megalodon (megalodon.jp, "ウェブ魚拓"), a Japanese web archive.

A capture's address, https://megalodon.jp/<YYYY-MMDD-HHMM-SS>/<original URL>, opens
Megalodon's own page (its menus, in Japanese) with the snapshot in an iframe; the
snapshot itself is at https://megalodon.jp/ref/<YYYY-MMDD-HHMM-SS>/<original URL>. Short
links on gyo.tc open the same capture page, which names the long address in its og:url.

The snapshot is read with a plain request, not in the browser: every HTML snapshot sends
a top-level visitor without a referrer back to the capture page by script, so the browser
got Megalodon's menus (some 800 characters) instead of the archived page. A plain request
runs no script and gets the whole snapshot, its images inlined as data: URIs, in about
two seconds. A captured image or other file is served as itself. Megalodon keeps no
videos: a snapshot's player holds a dead blob: URL.

Its addresses must not be decoded (see `util.preprocess_url()`): the original URL's own
encoded parts -- an embed's `href=https%3A%2F%2F...%26id%3D...` -- decoded, name another
address, and Megalodon answers that it has no such capture.
"""

import contextlib
import html as html_lib
import logging
import re
from typing import Optional

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.common import RetrievalFailed
from scrapemm.common.exceptions import TargetUnavailableError
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.server.download.common import HEADERS
from scrapemm.server.download.images import decode_image
from scrapemm.server.download.requests import curl_get
from scrapemm.server.integrations.base import RetrievalIntegration
from scrapemm.server.util import to_scraped_content

logger = logging.getLogger("scrapeMM")

# A capture's address: the capture page, or its snapshot (`ref/`)
CAPTURE_URL = re.compile(r"^https?://(?:www\.)?megalodon\.jp/(?:ref/)?(\d{4}-\d{4}-\d{4}-\d{2})/(.+)$",
                         re.IGNORECASE)
OG_URL = re.compile(r'<meta[^>]+property="og:url"[^>]+content="([^"]+)"', re.IGNORECASE)
# What Megalodon says for a capture it does not have ("no capture found")
NOT_FOUND_MARKER = "魚拓が見つかりません"


class Megalodon(RetrievalIntegration):
    name = "Megalodon"
    domains = ["megalodon.jp", "gyo.tc"]

    async def _connect(self):
        self.connected = True  # Public archive: nothing to log in to

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        capture = await self._capture_url(url)
        match = CAPTURE_URL.match(capture)
        if not match:
            raise RetrievalFailed(f"{url} is no Megalodon capture (expected "
                                  f"megalodon.jp/<YYYY-MMDD-HHMM-SS>/<URL>).")
        timestamp, original = match.groups()
        snapshot = f"https://megalodon.jp/ref/{timestamp}/{original}"
        response = await curl_get(snapshot, impersonate="chrome124", timeout=30)
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if response.status_code == 404 or (
                "html" in content_type and NOT_FOUND_MARKER in response.text):
            raise TargetUnavailableError(f"Megalodon has no capture at {capture}.")
        if response.status_code != 200:
            raise RetrievalFailed(f"Megalodon answered the snapshot {snapshot} with HTTP "
                                  f"{response.status_code}.")

        if content_type.startswith("image/"):
            # A captured image, served as itself
            image = await decode_image(response.content, snapshot)
            if image is None:
                raise RetrievalFailed(f"Megalodon's capture at {capture} is no readable image.")
            return ScrapedContent(multimodal=MultimodalSequence([image]))
        if "html" not in content_type:
            raise RetrievalFailed(f"Megalodon's capture at {capture} is a {content_type} file, "
                                  f"which scrapeMM does not read.")

        async with contextlib.AsyncExitStack() as stack:
            session = kwargs.get("session") or await stack.enter_async_context(
                aiohttp.ClientSession(headers=HEADERS))
            # The original address resolves the snapshot's relative links
            content = await to_scraped_content(
                response.text, session=session, output_format=kwargs.get("output_format", "multimodal"),
                url=original, max_video_size=kwargs.get("max_video_size"))
        # Megalodon prefixes the archived page's title with "(cache)"
        if content.markdown:
            content.markdown = content.markdown.removeprefix("(cache)")
        return content

    async def _capture_url(self, url: str) -> str:
        """The capture's address: a gyo.tc short link's long one (the og:url of the
        capture page it opens), else the URL itself."""
        if "gyo.tc" not in url.lower():
            return url
        response = await curl_get(url, impersonate="chrome124", timeout=30)
        if match := OG_URL.search(response.text or ""):
            return html_lib.unescape(match.group(1))
        if CAPTURE_URL.match(str(response.url)):  # Redirected to the capture page
            return str(response.url)
        raise RetrievalFailed(f"The short link {url} leads to no Megalodon capture.")
