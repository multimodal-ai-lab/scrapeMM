import logging
import re
from typing import Optional
from urllib.parse import urlparse, parse_qs

import aiohttp
from ezmm import MultimodalSequence
from playwright.async_api import Error, TimeoutError, async_playwright
from playwright_stealth import Stealth

from scrapemm.download.common import HEADERS
from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.integrations.decodo import decodo
from scrapemm.secrets import get_secret
from scrapemm.util import to_multimodal_sequence

logger = logging.getLogger("scrapeMM")

# Limits for inlining media as data URIs to avoid excessive memory usage
MAX_IMAGE_BYTES = 15 * 1024 * 1024  # 15 MB
MAX_VIDEO_BYTES = 25 * 1024 * 1024  # 25 MB
INLINE_CONCURRENCY = 6

PERMA_CC_API_BASE = "https://api.perma.cc/v1/public/archives"


def _extract_guid(url: str) -> Optional[str]:
    """Extract the Perma.cc GUID (e.g. 'N8MR-NS96') from a perma.cc URL."""
    match = re.search(r"perma\.cc/([A-Za-z0-9]+-[A-Za-z0-9]+)", url)
    return match.group(1) if match else None


def _is_screenshot_request(url: str) -> bool:
    """Check if the URL explicitly requests the screenshot view (?type=image)."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    return params.get("type", [None])[0] == "image"


async def _get_archive_metadata(guid: str, session: aiohttp.ClientSession) -> Optional[dict]:
    """Fetch archive metadata from Perma.cc's public API."""
    api_url = f"{PERMA_CC_API_BASE}/{guid}/"
    try:
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logger.debug(f"Perma.cc API returned status {resp.status} for GUID {guid}")
    except Exception as e:
        logger.debug(f"Failed to fetch Perma.cc API metadata: {e}")
    return None


def _extract_iframe_src(html: str) -> Optional[str]:
    """Extract the rejouer.perma.cc iframe src URL from the Perma.cc wrapper HTML."""
    # Match iframe with class "archive-iframe" or src containing rejouer.perma.cc
    patterns = [
        re.compile(r'<iframe[^>]+class=["\'][^"\']*archive-iframe[^"\']*["\'][^>]+src=["\']([^"\']+)["\']', re.IGNORECASE),
        re.compile(r'<iframe[^>]+src=["\']([^"\']*rejouer\.perma\.cc[^"\']+)["\']', re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.search(html)
        if match:
            url = match.group(1)
            # Unescape HTML entities
            url = url.replace("&amp;", "&")
            return url
    return None


def _extract_screenshot_img_url(html: str, guid: str) -> Optional[str]:
    """Extract the actual screenshot image URL from the Perma.cc screenshot page HTML.

    The page embeds the screenshot in an <img> tag whose src points to
    something like: https://rejouer.perma.cc/replay-web-page/w/.../mp_/file:///GUID/cap.png
    """
    # Look for img src containing the GUID or cap.png
    patterns = [
        # Match src with the GUID in the path
        re.compile(r'<img[^>]+src=["\']([^"\']*' + re.escape(guid) + r'[^"\']*cap\.png[^"\']*)["\']', re.IGNORECASE),
        # Match any src on rejouer.perma.cc
        re.compile(r'<img[^>]+src=["\']([^"\']*rejouer\.perma\.cc[^"\']*)["\']', re.IGNORECASE),
        # Match any src containing cap.png
        re.compile(r'<img[^>]+src=["\']([^"\']*cap\.png[^"\']*)["\']', re.IGNORECASE),
        # Fallback: any img src that looks like a full URL
        re.compile(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


class PermaCC(RetrievalIntegration):
    name = "Perma.cc"
    domains = ["perma.cc"]

    async def _connect(self):
        # No dedicated connection to establish
        self.connected = True

    async def _get(self, url: str, **kwargs) -> Optional[MultimodalSequence]:
        guid = _extract_guid(url)
        if not guid:
            raise RuntimeError(f"Could not extract Perma.cc GUID from URL: {url}")

        async with aiohttp.ClientSession(headers=HEADERS) as session:
            # Fetch archive metadata to get the original URL
            metadata = await _get_archive_metadata(guid, session)
            original_url = None
            if metadata:
                # Top-level "url" field
                original_url = metadata.get("url")
                # Fallback: extract from captures array (role=primary)
                if not original_url:
                    for capture in metadata.get("captures", []):
                        if capture.get("role") == "primary" and capture.get("url"):
                            original_url = capture["url"]
                            break
                logger.debug(f"Perma.cc metadata keys: {list(metadata.keys())}, original_url={original_url}")

            # Strategy 1: Scrape the ORIGINAL URL via Decodo (media URLs will be normal)
            if original_url:
                logger.debug(f"Perma.cc archive of: {original_url}")
                result = await _scrape_original_via_decodo(original_url, session)
                if result and _has_media(result):
                    return result

            # Strategy 2: Scrape the Perma.cc page itself via Decodo and download media
            html = await _get_html_via_decodo(url, session)
            if html:
                result = await to_multimodal_sequence(html, remove_urls=False, session=session, url=url)
                if result and _has_media(result):
                    return result

            # Strategy 3: Use Playwright with media inlining (handles session-bound URLs)
            html = await get_record_html(url)
            if html:
                return await to_multimodal_sequence(html, remove_urls=False, session=session, url=url)

            # Strategy 4: Return Decodo text-only result if we got one earlier
            if original_url:
                result = await _scrape_original_via_decodo(original_url, session)
                if result:
                    return result

            raise RuntimeError("Failed to retrieve Perma.cc record.")


def _has_media(seq: MultimodalSequence) -> bool:
    """Check if a MultimodalSequence contains any images or videos."""
    return bool(seq.images or seq.videos)


async def _scrape_original_via_decodo(
        original_url: str, session: aiohttp.ClientSession
) -> Optional[MultimodalSequence]:
    """Scrape the original archived URL via Decodo. Media URLs on the original
    site are normal HTTP URLs that can be downloaded directly."""
    try:
        result = await decodo.scrape(
            original_url,
            remove_urls=False,
            session=session,
            format="multimodal_sequence",
            enable_js=True,
            timeout=30,
        )
        if isinstance(result, MultimodalSequence):
            return result
    except Exception as e:
        logger.debug(f"Decodo scrape of original URL failed: {e}")
    return None


async def _get_html_via_decodo(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    """Fetch Perma.cc page HTML using Decodo proxy service to bypass Cloudflare."""
    try:
        result = await decodo.scrape(
            url,
            remove_urls=False,
            session=session,
            format="html",
            enable_js=True,
            timeout=30,
        )
        if isinstance(result, str) and result.strip():
            return result
    except Exception as e:
        logger.debug(f"Decodo scrape failed for Perma.cc: {e}")
    return None


async def _inline_media_in_frame(frame, image_limit: int = MAX_IMAGE_BYTES, video_limit: int = MAX_VIDEO_BYTES,
                                 concurrency: int = INLINE_CONCURRENCY) -> None:
    """Replace media URLs inside a frame with data URIs fetched using the same session.
    Operates directly in the page context to ensure session-bound URLs resolve.
    """
    try:
        await frame.evaluate(
            """
            async (opts) => {
              const maxImageBytes = opts.maxImageBytes ?? 15728640;
              const maxVideoBytes = opts.maxVideoBytes ?? 26214400;
              const concurrency = Math.max(1, Math.min(16, opts.concurrency ?? 6));

              const abs = (u) => {
                try { return new URL(u, document.baseURI).href; } catch (_) { return null; }
              };

              const pickFromSrcset = (srcset) => {
                if (!srcset) return null;
                // Choose the first candidate; simple and robust
                const first = srcset.split(',')[0]?.trim();
                if (!first) return null;
                const url = first.split(' ')[0]?.trim();
                return url || null;
              };

              const tasks = [];
              let videoTaskCount = 0;

              // Images (img[src] and img[srcset])
              document.querySelectorAll('img').forEach((img) => {
                let url = img.getAttribute('src');
                if (!url) {
                  const ss = img.getAttribute('srcset');
                  url = pickFromSrcset(ss);
                }
                if (url) {
                  const full = abs(url);
                  if (full) {
                    tasks.push({ el: img, attr: 'src', url: full, kind: 'image', cleanupSrcset: true });
                  }
                }
              });

              // Video poster images
              document.querySelectorAll('video[poster]').forEach((video) => {
                const url = video.getAttribute('poster');
                const full = abs(url);
                if (full) tasks.push({ el: video, attr: 'poster', url: full, kind: 'image' });
              });

              // Video sources: <video src> and <video><source src>
              document.querySelectorAll('video[src]').forEach((video) => {
                const url = video.getAttribute('src');
                const full = abs(url);
                if (full) { tasks.push({ el: video, attr: 'src', url: full, kind: 'video' }); videoTaskCount++; }
              });
              document.querySelectorAll('video source[src]').forEach((source) => {
                const url = source.getAttribute('src');
                const full = abs(url);
                if (full) { tasks.push({ el: source, attr: 'src', url: full, kind: 'video' }); videoTaskCount++; }
              });

              const isStreaming = (url, contentType) => {
                if (!url) return false;
                const u = url.toLowerCase();
                if (u.endsWith('.m3u8') || u.includes('m3u8')) return true;
                const ct = (contentType || '').toLowerCase();
                return ct.includes('mpegurl') || ct.includes('application/vnd.apple.mpegurl');
              };

              const ab2b64 = (buf) => {
                const bytes = new Uint8Array(buf);
                let binary = '';
                const chunk = 0x8000; // 32k chunks to avoid call stack limits
                for (let i = 0; i < bytes.length; i += chunk) {
                  const sub = bytes.subarray(i, i + chunk);
                  binary += String.fromCharCode.apply(null, sub);
                }
                return btoa(binary);
              };

              const fetchToDataURL = async (url, kind) => {
                const res = await fetch(url, { credentials: 'include' });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const contentType = res.headers.get('content-type') || '';
                const contentLengthHeader = res.headers.get('content-length');
                const limit = kind === 'image' ? maxImageBytes : maxVideoBytes;
                if (contentLengthHeader) {
                  const len = parseInt(contentLengthHeader);
                  if (!Number.isNaN(len) && len > limit) {
                    return { skipped: true, reason: 'too_large_precheck', contentType };
                  }
                }
                if (isStreaming(url, contentType)) {
                  return { skipped: true, reason: 'streaming', contentType };
                }
                const blob = await res.blob();
                if (blob.size > limit) {
                  return { skipped: true, reason: 'too_large', contentType: blob.type || contentType };
                }
                const buf = await blob.arrayBuffer();
                const b64 = ab2b64(buf);
                const mime = blob.type || contentType || 'application/octet-stream';
                return { dataURL: `data:${mime};base64,${b64}`, contentType: mime };
              };

              let idx = 0;
              let inlined = 0;
              let skipped = 0;
              let videoInlined = 0;

              const worker = async () => {
                while (true) {
                  const i = idx++;
                  if (i >= tasks.length) break;
                  const t = tasks[i];
                  try {
                    const res = await fetchToDataURL(t.url, t.kind);
                    if (res && res.dataURL) {
                      t.el.setAttribute(t.attr, res.dataURL);
                      if (t.cleanupSrcset) t.el.removeAttribute('srcset');
                      inlined++;
                      if (t.kind === 'video') videoInlined++;
                    } else {
                      skipped++;
                    }
                  } catch (_) {
                    skipped++;
                  }
                }
              };

              const workers = Array.from({ length: concurrency }, () => worker());
              await Promise.all(workers);

              // If no video was inlined via direct <video/src> or <source>,
              // attempt a TikTok-specific fallback by parsing the hydration JSON
              // and fetching an MP4 using the same session (Perma SW rewrites requests).
              const tryInlineTikTok = async () => {
                try {
                  const sc = document.querySelector('#__UNIVERSAL_DATA_FOR_REHYDRATION__');
                  if (!sc || !sc.textContent) return false;
                  let j;
                  try { j = JSON.parse(sc.textContent); } catch (_) { return false; }
                  const v = j?.__DEFAULT_SCOPE__?.["webapp.video-detail"]?.itemInfo?.itemStruct?.video;
                  if (!v) return false;
                  const cand = [];
                  const pushUrl = (u) => {
                    if (!u) return;
                    try {
                      const href = abs(u);
                      if (!href) return;
                      if (isStreaming(href)) return; // skip HLS
                      cand.push(href);
                    } catch (_) { /* noop */ }
                  };
                  pushUrl(v.playAddr);
                  pushUrl(v.downloadAddr);
                  if (Array.isArray(v.bitrateInfo)) {
                    for (const bi of v.bitrateInfo) {
                      const list = bi?.PlayAddr?.UrlList;
                      if (Array.isArray(list)) {
                        for (const u of list) pushUrl(u);
                      }
                    }
                  }
                  // de-dup
                  const seen = new Set();
                  const urls = cand.filter(u => (seen.has(u) ? false : (seen.add(u), true)));
                  for (const u of urls) {
                    try {
                      const res = await fetch(u, { credentials: 'include' });
                      if (!res.ok) continue;
                      const ct = (res.headers.get('content-type') || '').toLowerCase();
                      if (!ct.includes('video')) {
                        // still allow if URL looks like mp4
                        if (!u.toLowerCase().includes('.mp4')) continue;
                      }
                      const lenH = res.headers.get('content-length');
                      if (lenH) {
                        const len = parseInt(lenH);
                        if (!Number.isNaN(len) && len > maxVideoBytes) continue;
                      }
                      const blob = await res.blob();
                      if (blob.size > maxVideoBytes) continue;
                      const buf = await blob.arrayBuffer();
                      const b64 = ab2b64(buf);
                      const mime = blob.type || ct || 'video/mp4';
                      const dataURL = `data:${mime};base64,${b64}`;
                      let vEl = document.querySelector('video');
                      if (!vEl) {
                        vEl = document.createElement('video');
                        vEl.setAttribute('controls', '');
                        vEl.setAttribute('preload', 'metadata');
                        // Try to place near app root if present
                        const host = document.querySelector('#app') || document.body;
                        if (host.firstChild) host.insertBefore(vEl, host.firstChild); else host.appendChild(vEl);
                      } else {
                        // Remove <source> children to avoid conflicts
                        vEl.querySelectorAll('source').forEach(s => s.remove());
                      }
                      vEl.setAttribute('src', dataURL);
                      return true;
                    } catch (_) {
                      // try next candidate
                      continue;
                    }
                  }
                  return false;
                } catch (_) { return false; }
              };

              if (videoInlined === 0) {
                try { await tryInlineTikTok(); } catch (_) { /* ignore */ }
              }

              return { total: tasks.length, inlined, skipped, videoInlined };
            }
            """,
            {
                "maxImageBytes": int(image_limit),
                "maxVideoBytes": int(video_limit),
                "concurrency": int(concurrency),
            },
        )
    except Exception:
        # Best-effort; if anything fails, just proceed without inlining
        pass


async def get_record_html(url: str) -> str | None:
    """Fallback: Retrieves the HTML of the record saved by Perma.cc using Playwright.
    Loads the contents dynamically and returns any contained media as data URIs (base64-encoded)."""
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--window-position=-10000,-10000"],
        )
        context = await browser.new_context(
            accept_downloads=False,
            user_agent=HEADERS["User-Agent"],
        )
        page = await context.new_page()

        try:
            # Give a bit more time for Perma.cc to bootstrap and attach the iframe
            await page.goto(url, timeout=30000)
            await page.wait_for_load_state("domcontentloaded")  # 'domcontentloaded'
        except (TimeoutError, Error) as e:
            logger.warning(f"\rUnable to load page at URL '{url}'.\n\tReason: {type(e).__name__} {e}")
            return

        # Prefer the content of the Perma.cc archive iframe specifically
        iframe_html: str | None = None
        try:
            try:
                iframe_el = await page.wait_for_selector("iframe.archive-iframe", timeout=5000)
            except TimeoutError:
                iframe_el = None

            if iframe_el is not None:
                outer_frame = await iframe_el.content_frame()
                if outer_frame is not None:
                    # Ensure the outer iframe document is ready
                    try:
                        await outer_frame.wait_for_load_state("domcontentloaded", timeout=15000)
                    except TimeoutError:
                        pass

                    # Inside the outer iframe there's a direct child
                    # custom element <replay-web-page> which hosts the inner iframe.
                    # We target that inner iframe and return its document HTML.
                    try:
                        inner_iframe_el = await outer_frame.wait_for_selector(
                            "replay-web-page iframe", timeout=15000
                        )
                    except TimeoutError:
                        inner_iframe_el = None

                    if inner_iframe_el is not None:
                        inner_frame = await inner_iframe_el.content_frame()
                        if inner_frame is not None:
                            try:
                                await inner_frame.wait_for_load_state("domcontentloaded", timeout=15000)
                            except TimeoutError:
                                pass

                            # There is a third nested iframe somewhere under a
                            # <replay-app-main> element (not necessarily a direct child).
                            # Prefer that deepest iframe if present.
                            try:
                                deepest_iframe_el = await inner_frame.wait_for_selector(
                                    "replay-app-main iframe", timeout=15000
                                )
                            except TimeoutError:
                                deepest_iframe_el = None

                            if deepest_iframe_el is not None:
                                deepest_frame = await deepest_iframe_el.content_frame()
                                if deepest_frame is not None:
                                    try:
                                        await deepest_frame.wait_for_load_state(
                                            "domcontentloaded", timeout=15000
                                        )
                                    except TimeoutError:
                                        pass
                                    # Inline media in the deepest frame before exporting HTML
                                    await _inline_media_in_frame(deepest_frame)
                                    iframe_html = await deepest_frame.content()

                            # Fallback to the middle (inner) iframe content if deepest not found
                            if not iframe_html:
                                # Inline media in the middle frame before exporting HTML
                                await _inline_media_in_frame(inner_frame)
                                iframe_html = await inner_frame.content()

                    # Fallback: if inner iframe not found, use the outer iframe content
                    if not iframe_html:
                        # Inline media in the outer frame before exporting HTML
                        await _inline_media_in_frame(outer_frame)
                        iframe_html = await outer_frame.content()
        finally:
            # If the target iframe isn't available, fall back to the full page HTML
            if not iframe_html:
                iframe_html = await page.content()

            await page.close()
            await browser.close()
            return iframe_html
