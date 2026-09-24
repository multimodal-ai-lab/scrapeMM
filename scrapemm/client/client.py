"""The client half of scrapeMM: everything `retrieve()` does is ask a server.

The server streams one NDJSON line per URL as that URL finishes, rather than replying
once the whole batch is done. That keeps the progress bar honest on a batch that takes
minutes, and it lets each page's media be fetched while the remaining URLs are still
being scraped.
"""

import json
import logging
from typing import Any, AsyncIterator, Collection, Literal, Optional

import aiohttp
from tqdm import tqdm

from scrapemm.common import APP_NAME, OUTPUT_FORMATS, OutputFormat, ScrapingResponse
from scrapemm.common.exceptions import ServerError
from scrapemm.common.wire import PROTOCOL_VERSION, RegistryInfo, ResponsePayload
from .media import resolve_content
from .settings import Settings, settings

logger = logging.getLogger(APP_NAME)

CHUNK_SIZE = 64 * 1024


def _headers(config: Settings) -> dict[str, str]:
    headers = {"Accept": "application/x-ndjson"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return headers


async def retrieve(
        urls: str | Collection[str],
        show_progress: bool = True,
        actions: list[dict] | None = None,
        methods: Literal["auto"] | list[str] | list[Literal["auto"] | list[str]] = "auto",
        output_format: OutputFormat = "multimodal",
        include_media: bool = True,
        max_video_size: int | None = None,
        prioritize: Literal["completeness", "speed"] = "completeness",
        use_cache: bool = True,
        hedging_delay: float | None = None,
        config: Optional[Settings] = None,
) -> ScrapingResponse | list[ScrapingResponse]:
    """Retrieves the contents present at the given URL(s) through a scrapeMM server.

    Point the client at its server once, either with `scrapemm.configure(api_url=...,
    api_key=...)` or through the SCRAPEMM_API_URL / SCRAPEMM_API_KEY environment
    variables.

    :param urls: The URL(s) to retrieve.
    :param show_progress: Whether to show a progress bar while retrieving URLs.
    :param actions: A list of actions to perform on the webpage before scraping.
    :param methods: List of retrieval methods to use in order, or "auto" to let the
        server pick the best ones per domain. Available methods:
        - "integrations" (API integrations for Twitter, Instagram, etc.)
        - "firecrawl" (Firecrawl scraping service)
        - "decodo" (Decodo Web Scraping API)
        Pass a list of strings to apply one order to every URL, or a list of lists to
        give each URL of the batch its own.
    :param output_format: The format the content is needed in. Retrieval counts as
        successful only if this format could be produced (see ScrapingResponse.success).
        Available formats:
        - "multimodal" (response.content.multimodal: MultimodalSequence containing the
          Markdown text of the page along with the media downloaded from it)
        - "markdown" (response.content.markdown: string containing the scraped text in
          Markdown format, media referenced by hyperlink, nothing downloaded)
        - "html" (response.content.html: string containing the raw HTML code of the page)
    :param include_media: Deprecated, use output_format instead. If False, the
        "multimodal" output format is downgraded to "markdown".
    :param max_video_size: Maximum size of videos to download, in bytes.
    :param prioritize: "completeness" (higher timeouts, more retries) or "speed".
    :param use_cache: Whether the server may serve a recent retrieval of the same URL
        instead of scraping it again.
    :param hedging_delay: Seconds of head start each retrieval method gets before the
        next one is launched alongside it. None uses the server's configured default.
    :param config: Connection settings to use instead of the global ones.
    """
    assert isinstance(urls, (str, list)), "'urls' must be a string or a list of strings."
    assert output_format in OUTPUT_FORMATS, \
        f"Unknown output format '{output_format}'. Allowed: {list(OUTPUT_FORMATS)}"

    if not include_media:
        logger.warning("The 'include_media' parameter is deprecated. Use "
                       "output_format='markdown' instead.")
        if output_format == "multimodal":
            output_format = "markdown"

    single_url = isinstance(urls, str)
    requested: list[str] = [urls] if single_url else list(urls)

    if not requested:
        return []

    config = config or settings
    payload = {
        "urls": requested,
        "actions": actions,
        "methods": methods,
        "output_format": output_format,
        "max_video_size": max_video_size,
        "prioritize": prioritize,
        "use_cache": use_cache,
        "hedging_delay": hedging_delay,
    }

    by_url: dict[str, ScrapingResponse] = {}
    progress = None

    timeout = aiohttp.ClientTimeout(total=None, sock_read=config.read_timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        registry = RegistryInfo()
        async for message in _stream(session, config, payload):
            kind = message.get("type")

            if kind == "header":
                _check_protocol(message)
                registry = RegistryInfo.from_dict(message.get("registry") or {})
                if show_progress and message.get("total", 0) > 1:
                    progress = tqdm(total=message["total"], desc="Retrieving URLs...")

            elif kind == "result":
                response = await _to_response(
                    ResponsePayload.from_dict(message["payload"]),
                    registry, session, config)
                by_url[response.url] = response
                if progress is not None:
                    progress.update(1)

            elif kind == "error":
                raise ServerError(message.get("message", "The server reported an error."))

        if progress is not None:
            progress.close()

    results = [by_url.get(url) or _missing(url, output_format) for url in requested]
    return results[0] if single_url else results


async def _stream(session: aiohttp.ClientSession, config: Settings,
                  payload: dict) -> AsyncIterator[dict[str, Any]]:
    """Yields the server's NDJSON messages as they arrive."""
    url = f"{config.base_url}/v1/retrieve"
    try:
        async with session.post(url, json=payload, headers=_headers(config)) as response:
            if response.status == 401:
                raise ServerError(
                    f"{url} rejected the API key. Set it with "
                    f"scrapemm.configure(api_key=...) or SCRAPEMM_API_KEY.")
            if response.status >= 400:
                message, explained = _describe_failure(url, response, await response.text())
                # A current scrapeMM server explains its 404s and 405s; an unexplained
                # one means something else answered, which is worth finding out
                if response.status in (404, 405) and not explained and not response.history:
                    message += await _identify(session, config.base_url)
                raise ServerError(message)

            # Read raw chunks and split lines here rather than iterating
            # `response.content`, whose line reader refuses anything over 512 KB. One
            # result line carries a whole page's HTML, so that limit is reached by
            # perfectly ordinary pages.
            buffer = b""
            async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    if message := _parse(line):
                        yield message
            if message := _parse(buffer):
                yield message
    except aiohttp.ClientError as e:
        raise ServerError(
            f"Could not reach the scrapeMM server at {config.base_url}: {e}") from e


def _describe_failure(url: str, response: aiohttp.ClientResponse,
                      body: str) -> tuple[str, bool]:
    """Turns an error response into a message that says what to fix, and tells whether
    the server gave an explanation of its own."""
    detail = None
    try:
        detail = json.loads(body).get("detail")
    except (json.JSONDecodeError, AttributeError):
        pass
    # A bare "Method Not Allowed" repeats the status line and explains nothing
    informative = isinstance(detail, str) and detail != response.reason
    message = f"POST {url} failed with {response.status} {response.reason}"
    if informative:
        message += f": {detail}"
    elif not isinstance(detail, str) and body.strip():
        message += f": {body[:500]}"

    if response.history:
        final = response.url.with_path("/").with_query(None)
        message += (f". The request was redirected to {response.url}, which turns a POST "
                    f"into a GET. Set api_url to {str(final).rstrip('/')} directly.")
    return message, informative


async def _identify(session: aiohttp.ClientSession, base_url: str) -> str:
    """Says what actually answers at `base_url`. The health check needs no API key,
    so this works even when the key is wrong or missing."""
    service = None
    try:
        async with session.get(f"{base_url}/healthz", allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=5)) as response:
            health = await response.json(content_type=None)
            if isinstance(health, dict) and health.get("status") == "ok" and "version" in health:
                service = health["version"]
    except Exception:
        pass

    if service is not None:
        return (f". A scrapeMM server {service} runs at {base_url} but has no POST "
                f"/v1/retrieve; update the server or the client so their versions match.")
    return (f". Whatever answers at {base_url} is not a scrapeMM server (it has no "
            f"scrapeMM health check at /healthz), so another service probably holds "
            f"this port. Point the client at the scrapeMM server with "
            f"scrapemm.configure(api_url=...) or SCRAPEMM_API_URL, using the port set "
            f"as SCRAPEMM_PORT in the server's .env.")


def _parse(line: bytes) -> Optional[dict[str, Any]]:
    """One NDJSON line, or None for a blank or unreadable one."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        logger.warning(f"Skipping unreadable line from the server: {line[:200]!r}")
        return None


def _check_protocol(header: dict) -> None:
    spoken = header.get("protocol", 0)
    if spoken != PROTOCOL_VERSION:
        raise ServerError(
            f"The server speaks protocol version {spoken}, this client speaks "
            f"{PROTOCOL_VERSION}. Upgrade whichever of the two is older "
            f"(server version: {header.get('server_version', 'unknown')}).")


async def _to_response(payload: ResponsePayload, registry: RegistryInfo,
                       session: aiohttp.ClientSession,
                       config: Settings) -> ScrapingResponse:
    multimodal = None
    if payload.content is not None:
        multimodal = await resolve_content(
            payload.content, registry, session, config.base_url,
            _headers(config), config.media_transfer)
    return payload.to_response(multimodal=multimodal)


def _missing(url: str, output_format: OutputFormat) -> ScrapingResponse:
    """Stands in for a URL the server never reported on, so that the returned list
    still lines up with the URLs that were asked for."""
    return ScrapingResponse(
        url=url, content=None, output_format=output_format,
        errors={"scrapemm": ServerError("The server did not return a result for this URL.")})
