import asyncio
import logging
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import ClientResponseError, ClientConnectorError
from requests import ConnectionError, ReadTimeout
from requests.exceptions import RetryError

from scrapemm.server.config import get_config_var, update_config
from scrapemm.common.exceptions import (UnsupportedDomainError, TargetUnavailableError,
                                        AccessBlockedError, RetrievalFailed)
from scrapemm.common.scraping_response import ScrapedContent, OutputFormat
from scrapemm.server.download.common import HEADERS
from scrapemm.server.util import read_urls_from_file, get_domain, to_scraped_content

logger = logging.getLogger("scrapeMM")

# The instance that `docker compose up` starts alongside the server. It is the only
# default: guessing at localhost:3002 and friends only produced dashboards full of
# unreachable endpoints nobody had ever configured.
DEFAULT_FIRECRAWL_URLS = ["http://firecrawl-api:3002"]


def _normalize_firecrawl_url(url: str) -> str:
    url = str(url).strip()
    if url and not url.startswith("http"):
        url = "https://" + url
    return url


def configured_firecrawl_urls() -> list[str]:
    """The Firecrawl instances to use, in order of preference: the configured ones
    first, then the well-known defaults. Configuring several instances lets scrapeMM
    spread its scrapes across them, which is what lifts the throughput ceiling of a
    single self-hosted Firecrawl.

    Set them with `update_config(firecrawl_urls=["http://host-a:3002", ...])`. The
    older single-instance `firecrawl_url` setting keeps working.
    """
    configured: list[str] = []

    urls = get_config_var("firecrawl_urls") or []
    if isinstance(urls, str):  # Tolerate a single URL given to the plural setting
        urls = [urls]
    configured.extend(urls)

    if single := get_config_var("firecrawl_url"):
        configured.append(single)

    # Preserve order while dropping duplicates
    seen, ordered = set(), []
    for url in [_normalize_firecrawl_url(u) for u in configured] + DEFAULT_FIRECRAWL_URLS:
        if url and url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


FIRECRAWL_URLS = configured_firecrawl_urls()

NO_BOT_DOMAINS_FILE_PATH = Path(__file__).parent / "no_bot_domains.txt"
NO_BOT_DOMAINS = read_urls_from_file(NO_BOT_DOMAINS_FILE_PATH)

NO_AD_BLOCKING_DOMAINS = {
    "snopes.com"
}

async def locate_firecrawl() -> list[str]:
    """Scans the configured and well-known URLs for running Firecrawl instances.
    Returns every instance that responded, so that scrapes can be spread across them.

    Returns an empty list when none answers. It used to prompt for a URL instead, which
    a server cannot do: with no stdin, `input()` raises at once and the retry loop would
    spin forever. The Firecrawl endpoints are configured in the web UI now, and the
    dashboard reports which of them are reachable.
    """
    urls = await find_all_firecrawls(configured_firecrawl_urls())
    if not urls:
        logger.warning(
            f"❌ No Firecrawl instance is running at any of "
            f"{', '.join(configured_firecrawl_urls())}. Firecrawl scrapes will fail "
            f"until one is reachable; set the endpoints under Settings in the web UI.")
    return urls


async def find_all_firecrawls(urls: list[str]) -> list[str]:
    """Probes all candidate URLs concurrently and returns those that are running,
    keeping the given order of preference."""
    states = await asyncio.gather(*(get_firecrawl_state(url) for url in urls))
    return [url for url, state in zip(urls, states) if state == "running"]


class Firecrawl:
    """Wrapper around the AsyncFirecrawl class to handle pre- and post-processing.

    Holds one client per reachable Firecrawl instance and hands out scrapes in
    round-robin order. A single self-hosted Firecrawl is the throughput ceiling of
    the whole pipeline, so spreading the load across several of them is the cheapest
    way to raise it.
    """

    def __init__(self):
        self.n_scrapes = 0
        self.firecrawl_urls: list[str] = []
        self._clients: list = []
        self._next_client = 0

    @property
    def firecrawl_url(self) -> Optional[str]:
        """The primary instance. Kept for backwards compatibility."""
        return self.firecrawl_urls[0] if self.firecrawl_urls else None

    async def connect(self):
        from firecrawl import AsyncFirecrawl
        logging.getLogger("firecrawl").setLevel(logging.WARNING)

        urls = await locate_firecrawl()
        if self._clients:
            return  # Another coroutine connected while we were probing

        self.firecrawl_urls = urls
        if urls:
            logger.info(f"✅ Detected {len(urls)} Firecrawl instance(s) "
                        f"running at {', '.join(urls)}.")
        self._clients = [AsyncFirecrawl(api_url=url) for url in urls]

    def _pick_client(self) -> tuple[object, str]:
        """Returns the next client in round-robin order, along with its URL."""
        index = self._next_client % len(self._clients)
        self._next_client += 1
        return self._clients[index], self.firecrawl_urls[index]

    def _drop_instance(self, url: str) -> None:
        """Stops using an instance that went away, so that the remaining ones take over."""
        if url not in self.firecrawl_urls:
            return
        index = self.firecrawl_urls.index(url)
        del self.firecrawl_urls[index]
        del self._clients[index]
        logger.warning(f"Dropped Firecrawl instance {url}. "
                       f"{len(self._clients)} instance(s) left.")

    async def scrape(self,
                     url: str,
                     session: aiohttp.ClientSession,
                     output_format: OutputFormat = "multimodal",
                     max_attempts: int = 3,
                     max_video_size: int | None = None,
                     **kwargs) -> ScrapedContent:
        """Scrapes the given URL with Firecrawl. Returns the scraped HTML along with
        the requested output format. Media is downloaded only for the "multimodal"
        format. Raises an exception if the scraping failed."""

        domain = get_domain(url)
        if domain in NO_BOT_DOMAINS:
            raise UnsupportedDomainError(f"Firecrawl cannot scrape sites from {domain}")

        if not self._clients:
            await self.connect()

        if not self._clients:
            # `locate_firecrawl()` no longer prompts for a URL, so reaching here means
            # nothing is running. Saying so beats the ZeroDivisionError that picking a
            # client out of an empty pool used to raise.
            raise RetrievalFailed(
                f"No Firecrawl instance is reachable at "
                f"{', '.join(configured_firecrawl_urls())}. Configure one under "
                f"Settings in the web UI.")

        # Throw an exception for unavailable URLs which would otherwise cause Firecrawl
        # to get stuck in an infinite loop.
        # await self._ensure_availability(url, session)

        document = None
        for attempt in range(max_attempts):
            # Each attempt goes to the next instance, so a busy one is retried elsewhere
            client, instance_url = self._pick_client()
            try:
                document = await client.scrape(
                    url,
                    formats=["html"],
                    only_main_content=False,
                    remove_base64_images=False,
                    exclude_tags=["script", "style", "noscript", "footer", "aside"],
                    timeout=30_000,
                    wait_for=1_000,
                    store_in_cache=False,
                    block_ads=not domain in NO_AD_BLOCKING_DOMAINS,
                    **kwargs
                )
                break
            except Exception as e:
                # Ensure this instance is still running
                state = await get_firecrawl_state(instance_url)
                if state == "unavailable":
                    logger.error(f"❌ Firecrawl stopped running at {instance_url}.")
                    if len(self._clients) == 1:
                        raise RuntimeError("Firecrawl stopped running.")
                    self._drop_instance(instance_url)
                    if attempt >= max_attempts - 1:
                        raise e
                elif state == "busy":
                    if attempt < max_attempts - 1:
                        # With several instances the next attempt goes elsewhere, so
                        # there is nothing to wait for.
                        if len(self._clients) > 1:
                            logger.debug(f"Firecrawl at {instance_url} is busy. "
                                         f"Retrying on another instance...")
                        else:
                            logger.warning(f"⚠️ Firecrawl seems busy. Retrying in 10 seconds...")
                            await asyncio.sleep(10)
                    else:
                        raise e
                else:
                    raise e

        self.n_scrapes += 1
        html = document.html

        if not html:
            raise RuntimeError("No HTML content found in Firecrawl response.")

        return await to_scraped_content(html, session=session, output_format=output_format,
                                        url=url, max_video_size=max_video_size)

    async def _ensure_availability(self, url: str, session: aiohttp.ClientSession):
        """Probe if the URL is reachable. If an HTTP error >= 400 occurs, raise an exception."""
        try:
            async with session.head(url, timeout=2, headers=HEADERS) as response:
                response.raise_for_status()
                return  # All fine
        except (ReadTimeout, asyncio.TimeoutError):
            return  # We don't know yet
        except ClientResponseError as e:
            if isinstance(e.status, int) and (e.status >= 500 or e.status == 404):
                raise TargetUnavailableError(f"Error {e.status}: {e.message}")
            else:
                # logger.debug(f"Firecrawl skipping URL {url} due to unavailability: {e}")
                raise AccessBlockedError(f"Firecrawl could not access the content: Code {e.status} ({e.message})")
        except ClientConnectorError as e:
            # logger.debug(f"Firecrawl skipping URL {url} due to unavailability: {e}")
            raise TargetUnavailableError(f"Firecrawl could not connect to the target: {e}")


fire = Firecrawl()


async def firecrawl_is_running(url: str) -> bool:
    """Returns True iff Firecrawl can be successfully pinged at the specified URL."""
    return await get_firecrawl_state(url) == "running"


async def get_firecrawl_state(url: str) -> str | None:
    """Returns the state of Firecrawl at the specified URL."""
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        # Retrieve the head of the homepage
        try:
            async with session.head(url, timeout=2) as response:
                if 200 <= response.status < 400:
                    return "running"
        except (ReadTimeout, asyncio.TimeoutError):
            return "busy"
        except (aiohttp.ClientError, ConnectionError, RetryError):
            return "unavailable"
