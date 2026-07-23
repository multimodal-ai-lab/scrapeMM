import asyncio
import logging
import time
from typing import Optional

from playwright.async_api import TimeoutError, Page, Frame

from scrapemm.integrations.headed_browser import HeadedBrowser, ContentTarget

logger = logging.getLogger("scrapeMM")


# Limits for inlining media as data URIs to avoid excessive memory usage
MAX_IMAGE_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_VIDEO_BYTES = 250 * 1024 * 1024  # 250 MB
INLINE_CONCURRENCY = 6
# Large WARCs (e.g. 80MB+ Telegram videos) need a long wait for the innermost iframe.
INNERMOST_FRAME_TIMEOUT_MS = 120_000
INLINE_EVAL_TIMEOUT_MS = 180_000


class PermaCC(HeadedBrowser):
    name = "Perma.cc"
    domains = ["perma.cc"]

    # TODO: Implement PDF support, e.g., https://perma.cc/83VA-LTH9

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        # Check for Cloudflare challenge (passive check)
        body_text = await page.content()
        if "Just a moment" in body_text or "Performing security verification" in body_text:
            logger.info("\rCloudflare challenge detected. Waiting for UC mode to handle it...")
            try:
                # Move mouse slightly to simulate interaction if stuck
                await page.mouse.move(500, 500)
                # document.body can be null while the challenge page navigates/replaces the DOM.
                await page.wait_for_function(
                    """() => {
                        const body = document.body;
                        if (!body) return false;
                        const text = body.innerText || '';
                        return !text.includes('Just a moment')
                            && !text.includes('Performing security verification');
                    }""",
                    timeout=45000
                )
                logger.info("\rCloudflare challenge resolved.")
                await page.wait_for_timeout(2000)  # Wait for page to settle
            except TimeoutError:
                logger.warning("\rCloudflare challenge did not resolve in time.")

        # Prefer the content of the Perma.cc archive iframe specifically
        try:
            outer_iframe_el = await page.wait_for_selector("iframe.archive-iframe", timeout=5000)
        except TimeoutError:
            outer_iframe_el = None

        if not outer_iframe_el:
            return None

        outer_frame = await outer_iframe_el.content_frame()
        if not outer_frame:
            return None

        try:
            await outer_frame.wait_for_load_state("domcontentloaded", timeout=15000)
        except TimeoutError:
            pass

        # Inside the outer iframe there's a direct child
        # custom element <replay-web-page> which hosts the inner iframe.
        try:
            middle_iframe_el = await outer_frame.wait_for_selector(
                "replay-web-page iframe", timeout=15000
            )
        except TimeoutError:
            middle_iframe_el = None

        if not middle_iframe_el:
            logger.debug("Perma.cc middle iframe not found; falling back to outer iframe.")
            await _inline_media_in_frame(outer_frame)
            return outer_frame

        middle_frame = await middle_iframe_el.content_frame()
        if not middle_frame:
            logger.debug("Perma.cc middle iframe has no content frame; falling back to outer.")
            await _inline_media_in_frame(outer_frame)
            return outer_frame

        try:
            await middle_frame.wait_for_load_state("domcontentloaded", timeout=15000)
        except TimeoutError:
            pass

        # Critical: wait until the third (innermost) iframe under replay-app-main
        # has loaded usable archived content — do not fall back early on large WARCs.
        inner_frame = await self._wait_for_innermost_frame(middle_frame)
        if inner_frame is None:
            logger.debug("Perma.cc inner iframe not ready; falling back to middle iframe.")
            await _inline_media_in_frame(middle_frame)
            return middle_frame

        # Prefer a nested media-rich descendant (e.g. Telegram embed) when present.
        target = await self._pick_best_media_frame(inner_frame)
        await _inline_media_in_frame(target)
        return target

    async def _wait_for_innermost_frame(
            self, middle_frame: Frame, timeout_ms: int = INNERMOST_FRAME_TIMEOUT_MS
    ) -> Optional[Frame]:
        """Wait until replay-app-main's iframe exists and has usable document content."""
        deadline = time.monotonic() + timeout_ms / 1000
        inner_frame: Optional[Frame] = None

        while time.monotonic() < deadline:
            try:
                inner_iframe_el = await middle_frame.query_selector("replay-app-main iframe")
            except Exception:
                inner_iframe_el = None

            if inner_iframe_el is not None:
                try:
                    candidate = await inner_iframe_el.content_frame()
                except Exception:
                    candidate = None
                if candidate is not None:
                    inner_frame = candidate
                    if await self._frame_has_usable_content(inner_frame):
                        return inner_frame
            await asyncio.sleep(0.5)

        return inner_frame

    @staticmethod
    async def _frame_has_usable_content(frame: Frame) -> bool:
        try:
            info = await frame.evaluate(
                """() => {
                    if (!document.body || document.readyState === 'loading') {
                        return { ready: false };
                    }
                    const size = document.documentElement
                        ? document.documentElement.outerHTML.length
                        : 0;
                    const textLen = (document.body.innerText || '').trim().length;
                    const hasMedia = !!document.querySelector(
                        'img[src], video[src], video source[src], '
                        + '[style*="background-image"], '
                        + '.tgme_widget_message_photo_wrap, .tgme_widget_message_video'
                    );
                    const hasNested = document.querySelectorAll('iframe').length > 0;
                    return {
                        ready: size > 1000 || textLen > 40 || hasMedia || hasNested
                    };
                }"""
            )
            return bool(info and info.get("ready"))
        except Exception:
            return False

    async def _pick_best_media_frame(self, root: Frame) -> Frame:
        """Prefer a descendant frame that actually hosts post media (e.g. Telegram embed)."""
        deadline = time.monotonic() + 30
        best = root
        best_score = await self._media_score(root)

        while True:
            for frame in root.page.frames:
                if frame == root or not self._is_descendant_frame(frame, root):
                    continue
                score = await self._media_score(frame)
                if score > best_score:
                    best, best_score = frame, score

            # Strong media signal found (photo wrap / video / embed)
            if best_score >= 10:
                return best
            if time.monotonic() >= deadline:
                return best
            await asyncio.sleep(0.5)

    @staticmethod
    def _is_descendant_frame(frame: Frame, ancestor: Frame) -> bool:
        parent = frame.parent_frame
        while parent is not None:
            if parent == ancestor:
                return True
            parent = parent.parent_frame
        return False

    @staticmethod
    async def _media_score(frame: Frame) -> int:
        try:
            return int(
                await frame.evaluate(
                    """() => {
                        let score = 0;
                        const photos = document.querySelectorAll(
                            '.tgme_widget_message_photo_wrap[style*="background-image"]'
                        ).length;
                        const videos = document.querySelectorAll(
                            'video.tgme_widget_message_video[src], video[src]'
                        ).length;
                        const imgs = document.querySelectorAll('img[src]').length;
                        score += photos * 10 + videos * 10 + Math.min(imgs, 3);
                        if ((location.href || '').includes('embed=1')) score += 5;
                        return score;
                    }"""
                )
            )
        except Exception:
            return 0


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

              const isEligibleBgUrl = (url) => {
                if (!url || url.startsWith('data:')) return false;
                const u = url.toLowerCase();
                if (u.includes('/emoji/') || u.includes('/emojis/')) return false;
                if (/\\.(svg|svgz|eps)(\\?|$)/i.test(u)) return false;
                if (/\\.(jpe?g|png|gif|webp|bmp)(\\?|$)/i.test(u)) return true;
                if (u.includes('telegram-cdn.org/file/') || (u.includes('/file/') && u.includes('cdn'))) {
                  return true;
                }
                return false;
              };

              const bgUrlFromStyle = (style) => {
                if (!style) return null;
                const m = /background-image\\s*:\\s*url\\(\\s*['"]?([^'")\\s]+)['"]?\\s*\\)/i.exec(style);
                if (!m) return null;
                let url = m[1].trim();
                if (url.startsWith('//')) url = 'https:' + url;
                return isEligibleBgUrl(url) ? url : null;
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

              // CSS background images used as primary media (e.g. Telegram photo wraps)
              document.querySelectorAll('[style*="background-image"]').forEach((el) => {
                if (el.tagName === 'IMG' || el.tagName === 'VIDEO' || el.tagName === 'SOURCE') return;
                const url = bgUrlFromStyle(el.getAttribute('style') || '');
                if (!url) return;
                const full = abs(url);
                if (!full) return;
                // Materialize as <img> so downstream HTML parsers see a normal image src.
                const img = document.createElement('img');
                img.setAttribute('src', full);
                el.parentNode ? el.parentNode.insertBefore(img, el) : document.body.appendChild(img);
                el.remove();
                tasks.push({ el: img, attr: 'src', url: full, kind: 'image' });
              });

              // Video poster images
              document.querySelectorAll('video[poster]').forEach((video) => {
                const url = video.getAttribute('poster');
                const full = abs(url);
                if (full) tasks.push({ el: video, attr: 'poster', url: full, kind: 'image' });
              });

              // Prefer non-blurred Telegram videos when both variants exist
              const preferClearTelegramVideo = () => {
                const clear = document.querySelector('video.tgme_widget_message_video.js-message_video[src]');
                const blurred = document.querySelector('video.tgme_widget_message_video.js-message_video_blured[src]');
                if (clear && blurred) blurred.remove();
              };
              preferClearTelegramVideo();

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
        logger.debug("Perma.cc media inlining failed; continuing without data URIs.", exc_info=True)
