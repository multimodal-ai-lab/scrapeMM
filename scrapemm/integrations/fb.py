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

from scrapemm import RateLimitError
from scrapemm.common import CONFIG_DIR
from scrapemm.common.exceptions import ContentBlockedError, TargetUnavailableError, IPBannedError
from scrapemm.download import download_image
from scrapemm.download.common import HEADERS
from scrapemm.common.retrieval_integration import RetrievalIntegration
from scrapemm.integrations.ytdlp import get_content_with_ytdlp
from scrapemm.secrets import get_secret
from scrapemm.util import parse_netscape_cookies, postprocess_markdown

logger = logging.getLogger("scrapeMM")

VIDEO_URL_REGEX = r"facebook\.com/\d+/videos/\d+/?"
LIKE_COMMENT_SHARE_SVG_REGEX = (
    r"['\"]?%[0-9A-Fa-f]{2}.*?(?:%3C/svg%3E|%3C%2Fsvg%3E)['\"]?"
)
FB_PHOTO_HREF_REGEX = r'href="(https://www\.facebook\.com/photo/[^"]*)"'

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

    async def _get(self, url: str, **kwargs) -> MultimodalSequence:
        """Retrieves content from a Facebook post URL."""
        url = self._normalize_url(url)

        # Determine if this is a video or photo URL, act accordingly
        if self._is_video_url(url):
            try:
                return await self._get_video(url, **kwargs)
            except (ContentBlockedError, TargetUnavailableError):
                raise
            except Exception as e:
                if "No video formats found" in str(e):
                    raise ContentBlockedError("Video is blocked by Facebook.")
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
            if video:
                content.append(video)
        except (TargetUnavailableError, ContentBlockedError, IPBannedError):
            raise
        except Exception:
            pass

        try:
            image = await self._get_photo(url, **kwargs)
            if image:
                content.append(image)
        except Exception:
            pass

        try:
            from scrapemm.integrations.decodo import decodo
            text = await decodo.scrape(url, session=kwargs.get("session"), format="markdown", include_media=False)
            if text:
                content.append(text)
        except Exception:
            pass

        if content:
            return MultimodalSequence(content)

        try:
            return await self._get_user_profile(url, **kwargs)
        except Exception:
            pass

        raise TargetUnavailableError("Unable to retrieve content from Facebook URL.")

    async def _get_video(self, url: str, **kwargs) -> MultimodalSequence:
        """Retrieves content from a Facebook video URL."""
        if self.api_available:
            raise NotImplementedError(
                "Facebook video retrieval through API not yet supported."
            )
        else:
            return await get_content_with_ytdlp(
                url,
                platform="Facebook",
                # cookiefile=self.cookie_file.as_posix(),
                impersonate=ImpersonateTarget("chrome", "146"),
                **kwargs,
            )

    async def _get_photo(self, url: str, **kwargs) -> MultimodalSequence:
        """Retrieves content from a Facebook photo URL using Playwright with session cookies."""
        cookies = parse_netscape_cookies(self.cookie_file)

        if self._is_post_permalink(url):
            photos = await self._get_photos_from_post_permalink(
                url, cookies, **kwargs
            )
            return MultimodalSequence(photos)

        return await self._get_photo_from_regular_post(url, cookies)

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
            raise TargetUnavailableError("Could not locate image on Facebook photo page.")

        async with aiohttp.ClientSession(headers=HEADERS) as session:
            image = await download_image(image_url, session)

        if not image:
            raise TargetUnavailableError("Could not download image from Facebook photo.")

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

                except PlaywrightTimeoutError:
                    raise RuntimeError("Timed out loading Facebook photo page.")

                html = await page.content()
                photo_hrefs = self._collect_photo_hrefs_from_html(html)

            finally:
                await browser.close()

            photos = [
                await self._get_photo_from_regular_post(href, cookies)
                for href in photo_hrefs
            ]

            return [photo.images[0] for photo in photos if photo]

    def _is_post_permalink(self, url: str) -> bool:
        """Checks if the URL is a Facebook post permalink URL."""
        return re.search(r"facebook\.com/.+/posts/.+", url) is not None

    def _collect_photo_hrefs_from_html(self, html: str) -> list[str]:
        """Collects all photo hrefs from the given HTML string."""
        hrefs = re.findall(FB_PHOTO_HREF_REGEX, html)
        return hrefs

    async def _get_user_profile(self, url: str, **kwargs) -> MultimodalSequence:
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
