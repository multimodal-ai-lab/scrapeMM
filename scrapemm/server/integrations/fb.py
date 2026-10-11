import html as html_lib
import json
import logging
import asyncio
import contextlib
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup
from ezmm import MultimodalSequence
from ezmm.common.items import Image
from markdownify import markdownify as md
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from yt_dlp.networking.impersonate import ImpersonateTarget

from scrapemm.server.workers import run_light
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
FB_UNAVAILABLE_MARKERS = (
    '"tracePolicy":"comet.error"',  # "This content isn't available right now"
    '"tracePolicy":"comet.watch.video.not.found"',  # "This video is no longer available"
    '"currMedia":null',  # A photo page without its photo
)
FB_OG_IMAGE_REGEX = re.compile(r'<meta\s+property="og:image"\s+content="(.*?)"', re.DOTALL)
UNAVAILABLE_MESSAGE = ("The Facebook content is not available: it was removed, its "
                       "visibility was restricted, or it never existed.")

# Facebook's embed of a post or photo (plugins/post.php), made for other websites' readers
# and so public: since October 2026, Facebook redirects the server -- by its address,
# cookies or not -- to the login page for photo pages and permalink.php posts, but not for
# their embeds (nor for /<owner>/posts/<id> pages). At the widest embed Facebook renders,
# its images come in full resolution (up to 1440 px). Its text, though, is cut short
# ("See more"), so the text comes from the post's own page wherever that loads.
FB_EMBED_URL = "https://www.facebook.com/plugins/post.php?href={href}&show_text=true&width=750"
# What an embed of a removed or restricted post shows instead ("This Facebook post is no
# longer available"): its icon's class, and the message, in case the class changes
FB_EMBED_UNAVAILABLE_MARKERS = ('class="_1q3x', "no longer available")

# Query parameters of Facebook's embed plugins (plugins/post.php, plugins/video.php).
# Embed codes often carry the target in an unencoded 'href', so the target's own query
# parameters and the plugin's run together; these tell them apart.
FB_PLUGIN_PARAMS = {
    "href", "width", "height", "show_text", "t", "appid", "app_id", "show_captions",
    "autoplay", "mute", "allowfullscreen", "lazy", "adapt_container_width",
    "hide_cover", "show_facepile", "small_header", "tabs", "locale", "sdk",
    "container_width", "ref", "colorscheme", "layout", "size", "share", "action",
}

JS_GET_PHOTO_IMAGE = r"""
    () => {
        // Facebook's own graphics (static.xx.fbcdn.net/rsrc.php) are never the photo: its
        // login wall shows a 1240 px illustration, which was taken for the post's photo
        const own = (src) => /rsrc\.php|static\.xx\.fbcdn\.net/.test(src || '');

        // Strategy 1: data-visualcompletion attribute (legacy)
        let img = document.querySelector('img[data-visualcompletion="media-vc-image"]');
        if (img && !own(img.getAttribute('src'))) return img.getAttribute('src');

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
                if (src && w > 200 && !own(src)) return src;
            }
        }

        // Strategy 3: Largest image on the page with a CDN src
        const allImgs = Array.from(document.querySelectorAll('img[src*="scontent"], img[src*="fbcdn"]'))
            .filter(i => !own(i.getAttribute('src')));
        if (allImgs.length > 0) {
            allImgs.sort((a, b) => (b.naturalWidth || 0) - (a.naturalWidth || 0));
            const best = allImgs[0];
            if (best && (best.naturalWidth || 0) > 100) return best.getAttribute('src');
        }

        // Strategy 4: og:image meta tag
        const og = document.querySelector('meta[property="og:image"]');
        if (og && !own(og.getAttribute('content'))) return og.getAttribute('content');

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


def _is_facebook_graphic(url: str) -> bool:
    """Whether the image is one of Facebook's own graphics, not anybody's photo."""
    return "rsrc.php" in url or "static.xx.fbcdn.net" in url


def _shows_login_wall(html: str, page_url: str = "") -> bool:
    """Whether Facebook answered with its login wall ("Log in to Facebook") instead of the
    content: it redirected to /login, or shows the login form. Since October 2026 it does
    so to the server's browser for photo pages (in German, by the address)."""
    return urlparse(page_url).path.startswith("/login") or 'id="login_form"' in html \
        or 'name="login_form"' in html


def _is_unavailable_page(html: str) -> bool:
    """Whether Facebook served its "This content isn't available" page. Besides the
    explicit markers, it is recognized by what is missing: a page that loaded fine,
    with no login form, but without any post metadata and without a single video object."""
    return _shows_error_page(html) or (
            'id="login_form"' not in html
            and not FB_OG_TITLE_REGEX.search(html)
            and '"__typename":"Video"' not in html)


def _parse_embed(html: str) -> tuple[str, list[str], Optional[str]]:
    """Reads a post off Facebook's embed of it (see `FB_EMBED_URL`): its header (author,
    date, text) as Markdown, the URLs of its photos, and the URL of the post's own page
    (/<owner>/posts/<id>; for a photo, the post it belongs to) if the embed links one.
    The embed of a single photo and that of a post are laid out differently, but share
    these classes. Raises TargetUnavailableError if the embed says the post is gone."""
    soup = BeautifulSoup(html, "html.parser")
    posted = soup.select_one("abbr[data-utime]")
    text = soup.select_one(".userContent")
    if posted is None and text is None:
        if any(marker in html for marker in FB_EMBED_UNAVAILABLE_MARKERS):
            raise TargetUnavailableError(UNAVAILABLE_MESSAGE)
        raise RetrievalFailed("Facebook's embed of the post shows no post.")

    lines = ["**Facebook Post**"]
    if author := soup.select_one("._2_79"):
        lines.append(f"Author: {author.get_text(' ', strip=True)}")
    if posted is not None and posted.get("data-utime", "").isdigit():
        date = datetime.fromtimestamp(int(posted["data-utime"]), tz=timezone.utc)
        lines.append(f"Posted: {date:%Y-%m-%d %H:%M} UTC")
    header = "\n".join(lines)
    if text is not None and (caption := text.get_text("\n", strip=True)):
        # The embed cuts long texts short ("See more")
        if soup.select_one(".see_more_link"):
            caption += " …"
        header += f"\n\n{postprocess_markdown(caption)}"

    # The post's photos: Facebook's CDN images, but not the author's avatar (`_s0`) nor
    # Facebook's own graphics
    photos = []
    for img in soup.select("img"):
        src = img.get("src") or ""
        if ("scontent" in src or "fbcdn" in src) and not _is_facebook_graphic(src) \
                and "_s0" not in (img.get("class") or []) and src not in photos:
            photos.append(src)

    post_url = None
    for link in soup.select("a[href]"):
        target = urlparse(urljoin("https://www.facebook.com/", link["href"]))
        if target.netloc == "www.facebook.com" and re.fullmatch(r"/[^/]+/posts/[^/]+/?", target.path):
            post_url = f"https://www.facebook.com{target.path}"
            break
    return header, photos, post_url


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

        # Get the text first, straight from the page: it is public for public posts, and
        # complete (permalink.php URLs, which redirect to the login page, are rewritten to
        # their /<owner>/posts/<id> form by `_normalize_url()`)
        content = []
        html = await self._fetch_page(url)
        if html and _shows_error_page(html):
            raise TargetUnavailableError(UNAVAILABLE_MESSAGE)
        header = _extract_post_header(html or "")

        video = None
        try:
            video = await self._get_video(url, **kwargs)
        except Exception:
            pass

        # The photos, from the post's embed
        embed_header, photos = "", []
        try:
            embed_header, photos, _ = await self._get_from_embed(url, **kwargs)
        except TargetUnavailableError:
            if not header and not video:
                raise
        except Exception:
            logger.debug(f"Could not read Facebook's embed of {url}.", exc_info=True)
        if not photos and not video and (photo := await self._page_photo(html or "", **kwargs)):
            photos = [photo]  # A group's post has no embed, but its page names its photo

        # The page's text is the whole of it; the embed's, possibly cut short, only stands
        # in for a page that has none
        if header := header or embed_header:
            content.append(header)
        if video:
            content.append(video.multimodal)
        else:  # A video post's embed shows the video's thumbnail, which is no photo
            content.extend(photos)

        if not header:
            try:
                from scrapemm.server.integrations.decodo import decodo
                scraped = await decodo.scrape(url, session=kwargs.get("session"), output_format="markdown")
                content.append(scraped.markdown)
            except Exception:
                logger.debug(f"Could not retrieve the text of {url} via Decodo.", exc_info=True)

        if content:
            return ScrapedContent(multimodal=MultimodalSequence(content))

        if len(urlparse(url).path.strip("/").split("/")) == 1:  # facebook.com/<vanity>
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
        from scrapemm.server.download.requests import curl_get

        cookies = {c["name"]: c["value"]
                   for c in parse_netscape_cookies(self.cookie_file)}
        try:
            # Not curl_cffi's AsyncSession, which breaks the event loop's sockets
            response = await curl_get(url, cookies=cookies, impersonate="chrome124", timeout=30)
            return response.text if response.status_code == 200 else None
        except Exception:
            logger.debug(f"Could not fetch the Facebook page {url}.", exc_info=True)
            return None

    async def _get_photo(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves a Facebook photo: from its embed, or else from the photo page in a
        browser, with the session cookies if there are any."""
        try:
            header, photos, post_url = await self._get_from_embed(url, **kwargs)
            if photos:
                # The caption in full, from the page of the post the photo belongs to
                if post_url and (full := _extract_post_header(await self._fetch_page(post_url) or "")):
                    header = full
                return ScrapedContent(multimodal=MultimodalSequence([header, *photos]))
        except TargetUnavailableError:
            raise
        except Exception:
            logger.debug(f"Could not read Facebook's embed of {url}.", exc_info=True)

        cookies = parse_netscape_cookies(self.cookie_file)
        sequence = await self._get_photo_from_regular_post(url, cookies)
        return ScrapedContent(multimodal=sequence)

    async def _page_photo(self, html: str, **kwargs) -> Image | None:
        """The photo a post's page names as its og:image, if it is one: a post without a
        photo names none, or the author's avatar (`t39.30808-1`)."""
        match = FB_OG_IMAGE_REGEX.search(html)
        if not match:
            return None
        photo_url = html_lib.unescape(match.group(1))
        if "scontent" not in photo_url or _is_facebook_graphic(photo_url) \
                or "t39.30808-1/" in photo_url:
            return None
        async with contextlib.AsyncExitStack() as stack:
            session = kwargs.get("session") or await stack.enter_async_context(
                aiohttp.ClientSession(headers=HEADERS))
            return await download_image(photo_url, session)

    async def _get_from_embed(self, url: str, **kwargs) -> tuple[str, list[Image], Optional[str]]:
        """The post or photo as Facebook's embed shows it (see `FB_EMBED_URL`): its
        header as Markdown (the text possibly cut short), its photos, downloaded, and the
        URL of the post's own page, if linked."""
        html = await self._fetch_page(FB_EMBED_URL.format(href=quote(url, safe="")))
        if not html:
            raise RetrievalFailed(f"Could not load Facebook's embed of {url}.")
        header, photo_urls, post_url = await run_light(_parse_embed, html)
        photos = []
        async with contextlib.AsyncExitStack() as stack:
            session = kwargs.get("session") or await stack.enter_async_context(
                aiohttp.ClientSession(headers=HEADERS))
            for photo_url in photo_urls:
                # Kept only if large enough, like any image (see `MIN_MEDIA_SIDE`)
                if photo := await download_image(photo_url, session):
                    photos.append(photo)
        return header, photos, post_url

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
                page_url = page.url
                if not _shows_error_page(html):
                    image_url = await page.evaluate(JS_GET_PHOTO_IMAGE)

            finally:
                await browser.close()

        if _shows_error_page(html):
            raise TargetUnavailableError(UNAVAILABLE_MESSAGE)
        if image_url and _is_facebook_graphic(image_url):
            image_url = None
        if not image_url:
            if _shows_login_wall(html, page_url):
                raise AccessBlockedError("Facebook shows this photo only to logged-in users.")
            raise RetrievalFailed("Could not locate image on Facebook photo page.")

        async with aiohttp.ClientSession(headers=HEADERS) as session:
            image = await download_image(image_url, session)

        if not image:
            raise RetrievalFailed("Could not download image from Facebook photo.")

        # Retrieve text only. In a thread: converting Facebook's megabytes of markup held
        # the event loop -- every other request -- for seconds.
        def page_text() -> str:
            text = postprocess_markdown(md(html, heading_style="ATX"))
            # Remove SVG icons for like/comment/share
            return re.sub(LIKE_COMMENT_SHARE_SVG_REGEX, "", str(text))
        postprocessed_text = await run_light(page_text)

        return MultimodalSequence([image, postprocessed_text])

    async def _get_user_profile(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves a Facebook profile or page as a logged-out visitor sees it: name,
        the summary Facebook gives (followers, bio) and the profile picture, all read
        off the page's metadata."""
        html = await self._fetch_page(url)
        if not html:
            raise RetrievalFailed(f"Could not load the Facebook page for {url}.")
        if _shows_error_page(html):
            raise TargetUnavailableError(UNAVAILABLE_MESSAGE)
        name = FB_OG_TITLE_REGEX.search(html)
        if not name:
            raise AccessBlockedError("Facebook shows this profile only to logged-in users.")

        header = f"**Facebook Profile**\n{html_lib.unescape(name.group(1))}"
        if description := FB_OG_DESCRIPTION_REGEX.search(html):
            header += f"\n\n{postprocess_markdown(html_lib.unescape(description.group(1)))}"
        items: list = [header]
        if picture_url := FB_OG_IMAGE_REGEX.search(html):
            # Kept only if large enough, like any image (see `MIN_MEDIA_SIDE`)
            picture = await download_image(html_lib.unescape(picture_url.group(1)),
                                           kwargs.get("session"))
            if picture:
                items.append(picture)
        return ScrapedContent(multimodal=MultimodalSequence(items))

    def _normalize_url(self, url: str) -> str:
        """Turns the URL into the canonical www.facebook.com URL of the target: undoes
        HTML escaping ('&#038;' from WordPress embeds), unifies web./m./mbasic. hosts,
        and unwraps login redirects (login/?next=...) and embed plugins (plugins/post.php
        or plugins/video.php?href=...)."""
        url = html_lib.unescape(url.strip())
        # A URL pasted twice in a row ("https://…/reel/1https://…/reel/1"): keep the
        # first. Only in the path, since query strings may carry URLs legitimately.
        if doubled := re.match(r"(https?://[^?#]+?)https?://", url, flags=re.IGNORECASE):
            url = doubled.group(1)
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
        if parsed.path == "/permalink.php":
            # Redirects the server to the login page; the same post's /<owner>/posts/<id>
            # page does not
            query = parse_qs(parsed.query)
            story, owner = query.get("story_fbid", [""])[0], query.get("id", [""])[0]
            if story and owner:
                return f"https://www.facebook.com/{owner}/posts/{story}"
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
        return path_parts[0] in ("profile.php", "people")

    def _extract_username(self, url: str) -> str:
        """Extracts the username from a Facebook profile URL."""
        # url format: https://www.facebook.com/username<?...>
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.strip("/").split("/")
        if len(path_parts) > 0:
            return path_parts[0]
        return ""
