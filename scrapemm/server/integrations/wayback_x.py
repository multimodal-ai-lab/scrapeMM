"""X posts from the Wayback Machine's record of the X API's answer for them.

The Wayback Machine's captures of x.com post pages are only X's page without JavaScript
("JavaScript is not available", "Something went wrong"). But it also records posts as the
X API v2 answer (`data` + `includes.media/users`) under https://twitter.com/<user>/status/<id>
(mimetype application/json, timestamped at the post's creation), and often the video files
the post names (video.twimg.com/.../vid/avc1/<WxH>/<name>.mp4) too. That is the post
itself, text, author and media, as it was."""

import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urlencode, urlsplit

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.common.scraping_response import ScrapedContent, utc_iso
from scrapemm.server.download.common import HEADERS
from scrapemm.server.download.images import download_image
from scrapemm.server.download.requests import curl_get
from scrapemm.server.integrations.x import extract_tweet_id_from_url, tweet_time

logger = logging.getLogger("scrapeMM")

X_POST = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/[^/]+/status/\d+", re.IGNORECASE)


async def _cdx(params: dict, timeout: float = 45) -> list[list[str]]:
    for _ in range(2):  # The index is often slow or briefly offline
        try:
            response = await curl_get("https://web.archive.org/cdx/search/cdx?" + urlencode(params),
                                      impersonate="chrome124", timeout=timeout)
            if response.status_code == 200:
                return response.json()[1:] if response.text.strip() else []
        except Exception as e:
            logger.debug(f"CDX query {params.get('url')} failed: {type(e).__name__}: {e}")
        await asyncio.sleep(2)
    return []


async def _archived_api_answer(username: str, tweet_id: int) -> Optional[tuple[str, str, dict]]:
    """(timestamp, address, X API v2 answer) of the post as the Wayback Machine holds it."""
    for host in ("twitter.com", "x.com"):
        rows = await _cdx({"url": f"{host}/{username}/status/{tweet_id}", "output": "json",
                           "fl": "timestamp,original", "filter": "mimetype:application/json"})
        for timestamp, original in rows:
            try:
                response = await curl_get(f"https://web.archive.org/web/{timestamp}id_/{original}",
                                          impersonate="chrome124", timeout=60)
                answer = response.json()
            except Exception:
                continue
            if isinstance(answer, dict) and str((answer.get("data") or {}).get("id")) == str(tweet_id):
                return timestamp, original, answer
    return None


async def _captured_variants(variants: list[dict]) -> list[tuple[int, str]]:
    """(size, URL) of the post's mp4 variants the Wayback Machine holds, largest first, by
    the capture index (one query for the medium's folder)."""
    mp4s = [v["url"] for v in variants if v.get("content_type") == "video/mp4"]
    if not mp4s:
        return []
    folder = re.sub(r"/vid/.*$", "/vid/", mp4s[0].split("?", 1)[0])
    rows = await _cdx({"url": folder, "matchType": "prefix", "output": "json",
                       "fl": "original,statuscode,length", "filter": "mimetype:video/mp4"})
    wanted = {u.split("?", 1)[0] for u in mp4s}
    found: dict[str, int] = {}
    for original, status, length in rows:
        base = original.split("?", 1)[0]
        if status == "200" and base in wanted and length.isdigit():
            found[base] = max(found.get(base, 0), int(length))
    return sorted(((size, url) for url, size in found.items()), reverse=True)


async def x_post_from_wayback(url: str, max_video_size: Optional[int] = None
                              ) -> Optional[tuple[ScrapedContent, str, str]]:
    """The X post at `url` (an x.com or twitter.com post URL) with its media, from the
    Wayback Machine's record of the X API's answer; with that record's timestamp and
    address. None if it holds none."""
    from scrapemm.server.integrations.archive_org import _download_archived_video

    tweet_id, _ = extract_tweet_id_from_url(url)
    username = urlsplit(url).path.strip("/").split("/")[0]
    if not tweet_id or not username:
        return None
    found = await _archived_api_answer(username, tweet_id)
    if found is None:
        return None
    timestamp, address, answer = found
    tweet, includes = answer["data"], answer.get("includes") or {}
    users = {u["id"]: u for u in includes.get("users") or []}
    author = users.get(tweet.get("author_id")) or {"name": "?", "username": username}
    media_by_key = {m["media_key"]: m for m in includes.get("media") or []}
    keys = (tweet.get("attachments") or {}).get("media_keys") or []

    media, missing = [], []
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        for key in keys:
            medium = media_by_key.get(key) or {}
            item = None
            if medium.get("type") == "photo" and medium.get("url"):
                item = await download_image(f"https://web.archive.org/web/{timestamp}im_/{medium['url']}",
                                            session)
            elif medium.get("type") in ("video", "animated_gif"):
                captured = await _captured_variants(medium.get("variants") or [])
                # The best captured variant within the size limit; without the index,
                # each variant by bitrate (the archive redirects to its nearest capture)
                candidates = [u for size, u in captured if not max_video_size or size <= max_video_size] \
                    or [v["url"] for v in sorted(medium.get("variants") or [], key=lambda v: -v.get("bit_rate", 0))
                        if v.get("content_type") == "video/mp4"]
                for candidate in candidates:
                    item = await _download_archived_video(
                        f"https://web.archive.org/web/{timestamp}id_/{candidate}", session, max_video_size)
                    if item is not None:
                        break
                if item is None and medium.get("preview_image_url"):
                    missing.append("the video (its preview image instead)")
                    item = await download_image(
                        f"https://web.archive.org/web/{timestamp}im_/{medium['preview_image_url']}", session)
            if item is not None:
                media.append(item)
            else:
                missing.append(medium.get("type") or "a medium")

    metrics = tweet.get("public_metrics") or {}
    text = re.sub(r"https?://t\.co/\S+", "", tweet.get("text") or "").strip()
    posted = tweet.get("created_at", "")[:16].replace("T", " ")
    markdown = (f"**Post on X**\nAuthor: {author['name']}, @{author['username']}\n"
                f"Posted: {posted} UTC\n"
                f"Likes: {metrics.get('like_count')} - Retweets: {metrics.get('retweet_count')} - "
                f"Replies: {metrics.get('reply_count')} (as archived)\n\n{text}")
    if missing:
        markdown += f"\n\n[Not in the archive: {', '.join(missing)}]"
    content = ScrapedContent(markdown=markdown, multimodal=MultimodalSequence([markdown, *media]),
                             published_at=utc_iso(tweet_time(tweet_id)))
    return content, timestamp, address
