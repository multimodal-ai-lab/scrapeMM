import html as html_lib
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

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
FB_OG_TITLE_REGEX = re.compile(r'<meta\s+property="og:title"\s+content="(.*?)"', re.DOTALL)
FB_MESSAGE_TEXT_REGEX = re.compile(r'"message":\{"text":"((?:[^"\\]|\\.)*)"')
FB_CREATION_TIME_REGEX = re.compile(r'"creation_time":(\d+)')
FB_UNAVAILABLE_MARKERS = ('"tracePolicy":"comet.error"', '"currMedia":null')
UNAVAILABLE_MESSAGE = ("The Facebook content is not available: it was removed, its "
                       "visibility was restricted, or it never existed.")

# Query parameters of Facebook's embed plugins (plugins/post.php, plugins/video.php).
# Embed codes often carry the target in an unencoded 'href', so the target's own query
# parameters and the plugin's run together; these tell them apart.
FB_PLUGIN_PARAMS = {
    "href", "width", "height", "show_text", "t", "appid", "app_id", "show_captions",
    "autoplay", "mute", "allowfullscreen", "lazy", "adapt_container_width",
    "hide_cover", "show_facepile", "small_header", "tabs", "locale", "sdk",
    "container_width", "ref", "colorscheme", "layout", "size", "share", "action",
}

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
    """Returns the post's caption. og:description holds it, but truncated for long
    posts, so the full text is looked up in the embedded JSON by that prefix."""
    match = FB_OG_DESCRIPTION_REGEX.search(html)
    if not match:
        return ""
    description = html_lib.unescape(match.group(1))
    prefix = description.removesuffix("...").strip()[:60]
    if prefix:
        for raw in FB_MESSAGE_TEXT_REGEX.findall(html):
            try:
                text = json.loads(f'"{raw}"')
            except json.JSONDecodeError:
                continue
            if text.startswith(prefix):
                return postprocess_markdown(text)
    return postprocess_markdown(description)


def _shows_error_page(html: str) -> bool:
    """Whether the page is Facebook's "This content isn't available right now" (removed,
    private or restricted content), recognized by language-independent markers: the
    error route that post pages render, or a photo page whose photo came back null."""
    return any(marker in html for marker in FB_UNAVAILABLE_MARKERS)


def _is_unavailable_page(html: str) -> bool:
    """Whether Facebook served its "This content isn't available" page. Besides the
    explicit markers, it is recognized by what is missing: a page that loaded fine,
    with no login form, but without any post metadata and without a single video object."""
    return _shows_error_page(html) or (
            'id="login_form"' not in html
            and not FB_OG_TITLE_REGEX.search(html)
            and '"__typename":"Video"' not in html)


def _extract_post_header(html: str) -> str:
    """Returns the post's author, date and text as Markdown, read off the page itself.
    Empty if the page carries no post (e.g. a login wall)."""
    text = _extract_post_text(html)
    if not text:
        return ""
    lines = ["**Facebook Post**"]
    if author := FB_OG_TITLE_REGEX.search(html):
        lines.append(f"Author: {html_lib.unescape(author.group(1))}")
    if created := FB_CREATION_TIME_REGEX.search(html):
        date = datetime.fromtimestamp(int(created.group(1)), tz=timezone.utc)
        lines.append(f"Posted: {date:%Y-%m-%d %H:%M} UTC")
    return "\n".join(lines) + f"\n\n{text}"


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

        # Get the text first, straight from the page: it is public for public posts
        content = []
        html = await self._fetch_page(url)
        if html and _shows_error_page(html):
            raise TargetUnavailableError(UNAVAILABLE_MESSAGE)
        if header := _extract_post_header(html or ""):
            content.append(header)

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

        if not header:
            try:
                from scrapemm.server.integrations.decodo import decodo
                scraped = await decodo.scrape(url, session=kwargs.get("session"), output_format="markdown")
                content.append(scraped.markdown)
            except Exception:
                logger.debug(f"Could not retrieve the text of {url} via Decodo.", exc_info=True)

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
            message = str(e).lower()
            if "cannot parse data" not in message and "unable to parse" not in message:
                raise
            # A fact-check interstitial or an unavailable post, neither of which yt-dlp
            # can read; the page itself tells them apart
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
            if _is_unavailable_page(html):
                raise TargetUnavailableError(UNAVAILABLE_MESSAGE)
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

                html = await page.content()
                if not _shows_error_page(html):
                    image_url = await page.evaluate(JS_GET_PHOTO_IMAGE)

            finally:
                await browser.close()

        if _shows_error_page(html):
            raise TargetUnavailableError(UNAVAILABLE_MESSAGE)
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
        """Turns the URL into the canonical www.facebook.com URL of the target: undoes
        HTML escaping ('&#038;' from WordPress embeds), unifies web./m./mbasic. hosts,
        and unwraps login redirects (login/?next=...) and embed plugins (plugins/post.php
        or plugins/video.php?href=...)."""
        url = html_lib.unescape(url.strip())
        url = re.sub(r"^(?:https?://)?(?:(?:www|web|m|mbasic|touch)\.)?facebook\.com(?=[/?#]|$)",
                     "https://www.facebook.com", url, flags=re.IGNORECASE)
        parsed = urlparse(url)
        if parsed.netloc != "www.facebook.com":
            return url
        if parsed.path.rstrip("/") == "/login":
            target = parse_qs(parsed.query).get("next", [""])[0]
            return self._normalize_url(target) if target.startswith("http") else url
        if parsed.path.startswith("/plugins/"):
            target = self._extract_plugin_href(parsed.query)
            return self._normalize_url(target) if target.startswith("http") else url
        return url

    @staticmethod
    def _extract_plugin_href(query: str) -> str:
        """Returns the target URL of an embed plugin's query. The 'href' may be
        URL-encoded or not; in the latter case, the parameters following it belong to
        the target until one is a known plugin parameter."""
        parts = query.split("&")
        for i, part in enumerate(parts):
            if part.startswith("href="):
                href = [part.removeprefix("href=")]
                for following in parts[i + 1:]:
                    if following.split("=", 1)[0].lower() in FB_PLUGIN_PARAMS:
                        break
                    href.append(following)
                href = "&".join(href)
                return unquote(href) if re.match(r"https?%3A", href, re.I) else href
        return ""

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
