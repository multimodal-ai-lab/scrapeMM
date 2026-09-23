import html as html_lib
import logging
import re
from urllib.parse import parse_qs, urlparse

import aiohttp
from ezmm import MultimodalSequence
from ezmm.common.items import Image
from markdownify import markdownify as md
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from yt_dlp.networking.impersonate import ImpersonateTarget

from scrapemm.common import RateLimitError, RetrievalFailed
from scrapemm.server.paths import CONFIG_DIR
from scrapemm.common.exceptions import AccessBlockedError, TargetUnavailableError
from scrapemm.server.download import download_image, download_video
from scrapemm.server.download.common import HEADERS
from scrapemm.server.integrations.base import RetrievalIntegration
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.server.integrations.ytdlp import get_content_with_ytdlp
from scrapemm.server.secrets import get_secret
from scrapemm.server.util import parse_netscape_cookies, postprocess_markdown

logger = logging.getLogger("scrapeMM")

VIDEO_URL_REGEX = r"facebook\.com/\d+/videos/\d+/?"
LIKE_COMMENT_SHARE_SVG_REGEX = (
    r"['\"]?%[0-9A-Fa-f]{2}.*?(?:%3C/svg%3E|%3C%2Fsvg%3E)['\"]?"
)
FB_PHOTO_HREF_REGEX = r'href="(https://www\.facebook\.com/photo/[^"]*)"'

# Facebook wraps posts that fact-checkers flagged as false information in an
# interstitial. yt-dlp's extractor cannot parse those pages ("Cannot parse data"),
# yet the CDN URLs of the video are still embedded in them. Since flagged posts are
# exactly the material this library exists to collect, we dig them out ourselves.
# Keys are ordered best-quality first.
FB_VIDEO_URL_KEYS = (
    "browser_native_hd_url", "playable_url_quality_hd", "hd_src",  # HD
    "browser_native_sd_url", "playable_url", "sd_src",  # SD
)

# The page embeds JSON inside <script> tags, so the URL may be backslash-escaped
FB_VIDEO_URL_REGEXES = tuple(
    (key, re.compile(key + r'\\?"\s*:\s*\\?"((?:[^"\\]|\\.)*?)\\?"'))
    for key in FB_VIDEO_URL_KEYS
)
FB_OG_DESCRIPTION_REGEX = re.compile(
    r'<meta\s+property="og:description"\s+content="(.*?)"', re.DOTALL
)

JS_GET_PHOTO_IMAGE = """
    () => {
        // Strategy 1: data-visualcompletion attribute (legacy)
        let img = document.querySelector('img[data-visualcompletion="media-vc-image"]');
        if (img) return img.getAttribute('src');

        // Strategy 2: Large image inside the photo theater/spotlight viewer
        const viewerSelectors = [
            '[role="dialog"] img[src*="scontent"]',
            '[data-pagelet="MediaViewerPhoto"] img',
            '[role="main"] img[src*="scontent"]',
            'img[alt][src*="fbcdn"]',
            'img[alt][src*="scontent"]',
        ];
        for (const sel of viewerSelectors) {
            const candidates = document.querySelectorAll(sel);
            for (const c of candidates) {
                const src = c.getAttribute('src') || '';
                const w = c.naturalWidth || c.width || 0;
                if (src && w > 200) return src;
            }
        }

        // Strategy 3: Largest image on the page with a CDN src
        const allImgs = Array.from(document.querySelectorAll('img[src*="scontent"], img[src*="fbcdn"]'));
        if (allImgs.length > 0) {
            allImgs.sort((a, b) => (b.naturalWidth || 0) - (a.naturalWidth || 0));
            const best = allImgs[0];
            if (best && (best.naturalWidth || 0) > 100) return best.getAttribute('src');
        }

        // Strategy 4: og:image meta tag
        const og = document.querySelector('meta[property="og:image"]');
        if (og) return og.getAttribute('content');

        return null;
    }
""".strip()


def _unescape_json_url(raw: str) -> str:
    """Undoes the backslash escaping Facebook applies to URLs inside embedded JSON."""
    return raw.replace("\\/", "/").replace("\\u0025", "%")


def _extract_video_urls(html: str) -> list[str]:
    """Returns the video CDN URLs found in a Facebook page, best quality first
    and without duplicates."""
    urls: list[str] = []
    seen: set[str] = set()
    for _, regex in FB_VIDEO_URL_REGEXES:
        for match in regex.findall(html):
            candidate = _unescape_json_url(match)
            if candidate.startswith("http") and candidate not in seen:
                seen.add(candidate)
                urls.append(candidate)
    return urls


def _extract_post_text(html: str) -> str:
    """Returns the post's caption, taken from the og:description meta tag."""
    match = FB_OG_DESCRIPTION_REGEX.search(html)
    if not match:
        return ""
    return postprocess_markdown(html_lib.unescape(match.group(1)))


class Facebook(RetrievalIntegration):
    name = "Facebook"
    domains = ["facebook.com", "fb.watch"]
    cookie_file = CONFIG_DIR / "facebook_cookie.txt"

    async def _connect(self):
        self.api_available = False  # TODO

        cookie = get_secret("facebook_cookie")
        if cookie:
            # Save the cookie in a .txt file next to the secrets file
            with open(self.cookie_file, "w") as f:
                f.write(cookie)
            logger.info("✅ Using cookie to connect to Facebook.")
        else:
            logger.warning(
                "⚠️ Missing Facebook cookie. Won't be able to download videos that require login."
            )

        logger.info("✅ Facebook integration ready (yt-dlp only mode).")
        self.connected = True

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves content from a Facebook post URL."""
        url = self._normalize_url(url)

        # Determine if this is a video or photo URL, act accordingly
        if self._is_video_url(url):
            try:
                return await self._get_video(url, **kwargs)
            except (AccessBlockedError, TargetUnavailableError):
                raise
            except Exception as e:
                if "No video formats found" in str(e):
                    raise AccessBlockedError("Video is blocked by Facebook.")
                elif "This video is only available for registered users" in str(e):
                    raise RateLimitError(
                        "Facebook is rate-limiting your IP address. Set a 'facebook_cookie' in ScrapeMM."
                    )
                else:
                    raise e
        elif self._is_photo_url(url):
            return await self._get_photo(url, **kwargs)
        elif self._is_profile_url(url):
            return await self._get_user_profile(url, **kwargs)

        # The URL is not indicative, so try all methods

        # Get the text first
        content = []
        try:
            video = await self._get_video(url, **kwargs)
            content.append(video.multimodal)
        except Exception:
            pass

        try:
            image = await self._get_photo(url, **kwargs)
            content.append(image.multimodal)
        except Exception:
            pass

        try:
            from scrapemm.server.integrations.decodo import decodo
            scraped = await decodo.scrape(url, session=kwargs.get("session"), output_format="markdown")
            content.append(scraped.markdown)
        except Exception:
            pass

        if content:
            return ScrapedContent(multimodal=MultimodalSequence(content))

        try:
            return await self._get_user_profile(url, **kwargs)
        except Exception:
            pass

        raise RetrievalFailed("Unable to retrieve content from Facebook URL.")

    async def _get_video(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves content from a Facebook video URL."""
        if self.api_available:
            raise NotImplementedError(
                "Facebook video retrieval through API not yet supported."
            )

        try:
            sequence = await get_content_with_ytdlp(
                url,
                platform="Facebook",
                # cookiefile=self.cookie_file.as_posix(),
                impersonate=ImpersonateTarget("chrome", "146"),
                **kwargs,
            )
            return ScrapedContent(multimodal=sequence)
        except RetrievalFailed as e:
            if "unable to parse" not in str(e):
                raise
            # Most likely a fact-check interstitial, whose page yt-dlp cannot read
            logger.debug(f"yt-dlp could not parse {url}; reading the video off the page.")
            return await self._get_video_from_page(url, **kwargs)

    async def _get_video_from_page(self, url: str, **kwargs) -> ScrapedContent:
        """Extracts the video straight out of the post's HTML.

        Fallback for posts that yt-dlp's extractor chokes on -- in practice, those
        that Facebook covered with a fact-check interstitial. The player is hidden
        behind the warning, but the CDN URLs remain in the page source.
        """
        html = await self._fetch_page(url)
        if not html:
            raise RetrievalFailed(f"Could not load the Facebook page for {url}.")

        video_urls = _extract_video_urls(html)
        if not video_urls:
            raise RetrievalFailed(f"No video found in the Facebook page for {url}.")

        session = kwargs.get("session")
        max_video_size = kwargs.get("max_video_size")

        # Best quality first; fall back to the next candidate if one is unavailable
        # or exceeds the size limit.
        video = None
        for video_url in video_urls:
            video = await download_video(video_url, session=session,
                                         max_video_size=max_video_size)
            if video:
                break

        if not video:
            raise RetrievalFailed(f"Could not download the video of {url}.")

        text = _extract_post_text(html)
        items: list = [f"**Facebook Video**\n\n{text}" if text else "**Facebook Video**"]
        items.append(video)
        return ScrapedContent(multimodal=MultimodalSequence(items))

    async def _fetch_page(self, url: str) -> str | None:
        """Downloads the post's HTML with the stored session cookies, impersonating a
        real browser. Facebook serves aiohttp's TLS fingerprint a login wall."""
        from curl_cffi.requests import AsyncSession

        cookies = {c["name"]: c["value"]
                   for c in parse_netscape_cookies(self.cookie_file)}
        try:
            async with AsyncSession() as session:
                response = await session.get(url, cookies=cookies,
                                             impersonate="chrome124", timeout=30)
                return response.text if response.status_code == 200 else None
        except Exception:
            logger.debug(f"Could not fetch the Facebook page {url}.", exc_info=True)
            return None

    async def _get_photo(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves content from a Facebook photo URL using Playwright with session cookies."""
        cookies = parse_netscape_cookies(self.cookie_file)

        if self._is_post_permalink(url):
            photos = await self._get_photos_from_post_permalink(
                url, cookies, **kwargs
            )
            return ScrapedContent(multimodal=MultimodalSequence(photos))

        sequence = await self._get_photo_from_regular_post(url, cookies)
        return ScrapedContent(multimodal=sequence)

    async def _get_photo_from_regular_post(
            self, url, cookies: list[dict[str, str]]
    ) -> MultimodalSequence:
        image_url = None
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()
            try:
                try:
                    await page.goto(url, timeout=30000)
                    await page.wait_for_load_state("domcontentloaded")
                    # Wait for network to settle so images are rendered
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeoutError:
                        pass
                    # Give React/JS a moment to render the photo
                    await page.wait_for_timeout(2000)
                except PlaywrightTimeoutError:
                    raise TimeoutError("Timed out loading Facebook photo page.")

                image_url = await page.evaluate(JS_GET_PHOTO_IMAGE)

            except TimeoutError:
                raise

            finally:
                html = await page.content()
                await browser.close()

        if not image_url:
            raise RetrievalFailed("Could not locate image on Facebook photo page.")

        async with aiohttp.ClientSession(headers=HEADERS) as session:
            image = await download_image(image_url, session)

        if not image:
            raise RetrievalFailed("Could not download image from Facebook photo.")

        # Retrieve text only
        text = md(html, heading_style="ATX")
        postprocessed_text = postprocess_markdown(text)
        # Remove SVG icons for like/comment/share
        postprocessed_text = re.sub(
            LIKE_COMMENT_SHARE_SVG_REGEX, "", str(postprocessed_text)
        )

        return MultimodalSequence([image, postprocessed_text])

    async def _get_photos_from_post_permalink(
            self, url: str, cookies: list[dict[str, str]], **kwargs
    ) -> list[Image]:
        """Retrieves all photos from a Facebook post permalink URL."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()

            try:
                try:
                    await page.goto(url, timeout=30000)
                    await page.wait_for_load_state("domcontentloaded")
                    # The post's photo links are rendered by JS, so the markup right
                    # after domcontentloaded still lacks them. Let the page settle.
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeoutError:
                        pass
                    await page.wait_for_timeout(2000)

                except PlaywrightTimeoutError:
                    raise RuntimeError("Timed out loading Facebook photo page.")

                html = await page.content()
                photo_hrefs = self._collect_photo_hrefs_from_html(html)

            finally:
                await browser.close()

            photos = []
            for href in photo_hrefs:
                # One unavailable photo must not cost us the remaining ones
                try:
                    photos.append(await self._get_photo_from_regular_post(href, cookies))
                except Exception:
                    logger.debug(f"Could not retrieve the Facebook photo {href}.",
                                 exc_info=True)

            return [photo.images[0] for photo in photos if photo and photo.images]

    def _is_post_permalink(self, url: str) -> bool:
        """Checks if the URL is a Facebook post permalink URL."""
        return re.search(r"facebook\.com/.+/posts/.+", url) is not None

    def _collect_photo_hrefs_from_html(self, html: str) -> list[str]:
        """Collects all photo hrefs from the given HTML string, in page order and
        without duplicates. The hrefs are HTML-escaped in the markup ('&amp;'), which
        would turn the query parameters into garbage if left as is."""
        hrefs = (html_lib.unescape(href)
                 for href in re.findall(FB_PHOTO_HREF_REGEX, html))
        return list(dict.fromkeys(hrefs))

    async def _get_user_profile(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves content from a Facebook user profile URL."""
        raise NotImplementedError("No method available to retrieve Facebook profiles.")

    def _normalize_url(self, url: str) -> str:
        """If the URL is a login Facebook URL, i.e., of the form https://www.facebook.com/login/?next=...
        or https://www.facebook.com/plugins/post.php?href=..., extracts the actual post's URL."""
        if url.startswith(
                "https://www.facebook.com/login/?next="
        ):  # Login redirect URLs
            query = urlparse(url).query
            return parse_qs(query).get("next", [])[0] or url
        elif url.startswith(
                "https://www.facebook.com/plugins/post.php?href="
        ):  # Post embedding links
            query = urlparse(url).query
            return parse_qs(query).get("href", [])[0] or url
        return url

    def _is_video_url(self, url: str) -> bool:
        """Checks if the URL is a Facebook video URL."""
        # video URLS are in the format: https://www.facebook.com/watch?v=VIDEO_ID or fb.watch/...
        # or Reels: https://www.facebook.com/reel/REEL_ID
        return (
                "facebook.com/watch" in url
                or "facebook.com/reel" in url
                or bool(re.search(VIDEO_URL_REGEX, url))
                or "fb.watch" in url
                or "/videos/" in url
        )

    def _extract_video_id(self, url: str) -> str:
        """Extracts the video ID from a Facebook video URL."""
        parsed_url = urlparse(url)
        query_params = parsed_url.query
        for param in query_params.split("&"):
            if param.startswith("v="):
                return param.split("=")[1]
        return ""

    def _is_photo_url(self, url: str) -> bool:
        """Checks if the URL is a Facebook photo URL."""
        return "facebook.com/photo" in url or "facebook.com/photos" in url

    def _is_profile_url(self, url: str) -> bool:
        """Checks if the URL is a Facebook profile URL."""
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        return len(path_parts) > 0 and path_parts[0] == "profile.php"

    def _extract_username(self, url: str) -> str:
        """Extracts the username from a Facebook profile URL."""
        # url format: https://www.facebook.com/username<?...>
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.strip("/").split("/")
        if len(path_parts) > 0:
            return path_parts[0]
        return ""
