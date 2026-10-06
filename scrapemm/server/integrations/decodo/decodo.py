import asyncio
import json
import logging
from typing import Optional

import aiohttp
from aiohttp import ClientConnectorError

from scrapemm.common import (AccessBlockedError, QuotaExceededError, RateLimitError,
                             RegionBlockedError, RetrievalFailed, TargetUnavailableError,
                             UnsupportedDomainError)
from scrapemm.common.scraping_response import ScrapedContent, OutputFormat
from scrapemm.server.secrets import get_secret
from scrapemm.server.util import get_domain, to_scraped_content

logger = logging.getLogger("scrapeMM")

# Domains which require more advanced scraping
PREMIUM_PROXY_DOMAINS = {
    "snopes.com"
}


class Decodo:
    """Scrapes web content using Decodo's Web Scraping API with proxy support
    and JavaScript rendering capabilities."""

    DECODO_API_URL = "https://scraper-api.decodo.com/v2/scrape"
    # Where to fetch a page from that this server's region is denied (HTTP 451): the
    # pages concerned are mostly US sites blocking the EU
    UNBLOCKED_GEO = "United States"

    def __init__(self):
        self.basic_auth_token = None
        self.n_scrapes = 0

    def _load_token(self):
        """Loads Decodo credentials from the secrets manager."""
        self.basic_auth_token = get_secret("decodo_token")

        if self.basic_auth_token:
            logger.info("✅ Decodo token set.")
        else:
            logger.warning("⚠️ Decodo auth token not found. Please configure it in secrets.")

    def _has_token(self) -> bool:
        """Checks if Decodo credentials are available."""
        return bool(self.basic_auth_token)

    async def scrape(
            self, url: str,
            session: aiohttp.ClientSession,
            output_format: OutputFormat = "multimodal",
            enable_js: bool = True,
            timeout: int = 30,
            max_retries: int = 5,
            max_video_size: int | None = None,
            geo: str | None = None,
    ) -> ScrapedContent:
        """Downloads the contents of the specified webpage using Decodo's API.

        Args:
            url: The URL to scrape
            session: The aiohttp ClientSession to use
            output_format: The format the content is needed in (default: "multimodal").
                Media is downloaded only for the "multimodal" format.
            enable_js: Whether to enable JavaScript rendering (default: True)
            timeout: Request timeout in seconds (default: 30)
            max_retries: Maximum number of retries for failed requests (default: 5)
            max_video_size: Maximum size of videos embedded in the page, in bytes
            geo: The country to fetch the page from (Decodo's `geo`), e.g. "United States";
                Decodo's own choice if None

        Returns:
            ScrapedContent holding the scraped HTML along with the requested output format

        Raises:
            An exception if the scraping failed
        """
        if not self._has_token():
            self._load_token()

        if not self._has_token():
            logger.warning("⚠️ Cannot scrape with Decodo: credentials not configured.")
            raise RuntimeError("Decodo credentials not configured.")

        domain = get_domain(url)
        use_premium_proxy = domain in PREMIUM_PROXY_DOMAINS

        # Try with JS rendering first if enabled
        html = await self._call_decodo(url, session, enable_js, timeout=timeout, max_retries=max_retries,
                                       use_premium_proxy=use_premium_proxy, geo=geo)

        return await to_scraped_content(html, session=session, output_format=output_format,
                                        url=url, max_video_size=max_video_size)

    async def _call_decodo(
            self, url: str,
            session: aiohttp.ClientSession,
            enable_js: bool = True,
            timeout: int = 10,
            max_retries: int = 5,
            use_premium_proxy: bool = False,
            geo: str | None = None,
    ) -> str:
        """Calls the Decodo API to scrape the given URL with exponential backoff retry logic.

        Args:
            url: The URL to scrape
            session: The aiohttp ClientSession to use
            enable_js: Whether to enable JavaScript rendering
            timeout: Request timeout in seconds
            max_retries: Maximum number of retry attempts for rate limits (default: 5)
            use_premium_proxy: Whether to use premium proxies for scraping (default: False). Increases
                success rate but also increases cost (by a factor of 2 or more)

        Returns:
            HTML content as a string, or None if scraping failed
        """
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Basic {self.basic_auth_token}',
        }

        # Build request payload
        # Note: For simple URL scraping, we just provide the URL
        # The "target" parameter is only used for specific templates like "google_search"
        payload = {
            "url": url,
        }

        # Enable JavaScript rendering if requested
        # Note: This requires an Advanced plan subscription
        if enable_js:
            payload["headless"] = "html"

        if use_premium_proxy:
            payload["proxy_pool"] = "premium"

        if geo:
            payload["geo"] = geo

        # Retry loop with exponential backoff
        for attempt in range(max_retries + 1):
            try:
                async with session.post(
                        self.DECODO_API_URL,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=timeout)
                ) as response:
                    # Validate response health
                    if response.status != 200:
                        logger.debug(f"Communication with Decodo API failed. Status code: {response.status}")

                        if response.status == 429:  # Rate limit
                            if attempt >= max_retries:
                                logger.warning(f"Error 429: Rate limit hit and maximum retries reached.")
                                raise RateLimitError(f"Decodo rate limit hit (despite {max_retries} retries).")
                        elif response.status == 613:
                            if attempt >= max_retries:
                                raise RuntimeError(f"Decodo API error 613 (despite {max_retries} retries).")
                        elif response.status == 502:  # Bad gateway
                            if attempt >= max_retries:
                                logger.warning(f"Error 502: Bad gateway and maximum retries reached.")
                                raise RuntimeError(
                                    f"Decodo API error 502: Bad gateway (despite {max_retries} retries).")

                        else:  # Other errors that don't go away on retry
                            error = _api_error(response.status, response.reason,
                                               await response.text(), url)
                            logger.debug(f"Decodo refused {url}: {type(error).__name__}: {error}")
                            raise error

                    else:
                        # Parse response
                        json_response = await response.json()

                        # Validate if scrape was successful
                        if json_response.get("status") == "failed":
                            status_code = json_response.get("status_code")
                            message = json_response.get("message")
                            logger.info(f"Decodo failed to scrape {url}: Error {status_code}: {message}")
                            raise RetrievalFailed(f"Decodo failed with error {status_code}: {message}")

                        # Extract HTML content from results
                        if "results" in json_response and len(json_response["results"]) > 0:
                            result = json_response["results"][0]

                            # Check status code from the actual request
                            status_code = result.get("status_code")
                            if status_code and status_code >= 400:
                                msg = f"Target website returned status {status_code} for {url}"
                                logger.warning(msg)
                                if status_code in (404, 410):
                                    raise TargetUnavailableError(msg)
                                if status_code == 451:
                                    raise RegionBlockedError(msg + " (blocked in the proxy's region)")
                                raise RetrievalFailed(msg)

                            html_content = result.get("content")
                            if html_content:
                                self.n_scrapes += 1
                                logger.debug(f"Successfully scraped {url} with Decodo (scrape #{self.n_scrapes})")
                                return html_content
                            else:
                                msg = f"No content in Decodo response for {url}"
                                logger.warning(msg)
                                raise RetrievalFailed(msg)
                        else:
                            msg = f"No results in Decodo response for {url}"
                            logger.warning(msg)
                            logger.debug(f"Response: {json_response}")
                            raise RetrievalFailed(msg)

            except ClientConnectorError:  # Decodo sometimes has hiccups
                if attempt >= max_retries:
                    raise
                else:
                    logger.debug("Decodo API connection error. Retrying...")
            except aiohttp.ClientError as e:
                logger.error(f"Network error while scraping with Decodo: {e}")
                raise
            except (RateLimitError, asyncio.TimeoutError):
                raise
            except Exception as e:
                logger.debug(f"Error while scraping with Decodo: {e}")
                raise

            await backoff(attempt)  # Wait before retrying

        # Should not reach here
        raise RetrievalFailed("Failed to scrape with Decodo after multiple attempts.")


def _decodo_message(body: str) -> str:
    """Decodo's own explanation of an error, from its JSON body ({"status": "failed",
    "message": "Url is not supported."}), else the body itself."""
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:300]
    if isinstance(data, dict):
        for key in ("message", "detail", "error", "errors"):
            if data.get(key):
                return str(data[key]).strip()[:300]
    return body.strip()[:300]


def _api_error(status: int, reason: Optional[str], body: str, url: str) -> Exception:
    """Turns an error response of Decodo's API into an exception that says what went
    wrong -- and what to do about it -- rather than repeating the bare status line."""
    message = _decodo_message(body)
    said = f' Decodo says: "{message}"' if message else ""
    domain = get_domain(url)

    if status == 400 and "not supported" in message.lower():
        # A refusal by policy, not a malformed request: Decodo will not scrape this site
        return UnsupportedDomainError(f"Decodo does not scrape {domain or 'this URL'}; it "
                                      f"refuses the URL as unsupported.{said}")
    match status:
        case 400:
            return RetrievalFailed(f"Decodo rejected the request as invalid (400).{said} "
                                   f"A possible cause: scrapeMM asks for JavaScript "
                                   f"rendering, which Decodo refuses on plans below Advanced.")
        case 401:
            return RuntimeError(f"Decodo rejected the credentials (401).{said} Check the "
                                f"Decodo token under Secrets in the web UI.")
        case 402:
            return QuotaExceededError(f"Decodo requires payment (402): the subscription "
                                      f"ran out or does not cover this request.{said}")
        case 403:
            return AccessBlockedError(f"Decodo's account may not scrape this (403).{said}")
        case 408:
            return RetrievalFailed(f"{domain or 'The website'} did not answer Decodo in "
                                   f"time (408).{said}")
        case 500:
            return RetrievalFailed(f"Decodo failed internally (500).{said}")
    return RetrievalFailed(f"Decodo answered {status} {reason or ''}.".replace(" .", ".") + said)


async def backoff(n_past_attempts: int):
    """Exponential backoff: 2^n_past_attempts seconds (1s, 2s, 4s, 8s, 16s...)"""
    wait_time = 2 ** n_past_attempts
    logger.debug(f"Backing off for {wait_time:.0f}s before retrying...")
    await asyncio.sleep(wait_time)


# Create a singleton instance
decodo = Decodo()
