import asyncio
import logging
from typing import Optional

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.secrets import get_secret
from scrapemm.scraping.util import to_multimodal_sequence

logger = logging.getLogger("scrapeMM")


class Decodo:
    """Scrapes web content using Decodo's Web Scraping API with proxy support
    and JavaScript rendering capabilities."""

    DECODO_API_URL = "https://scraper-api.decodo.com/v2/scrape"

    def __init__(self):
        self.username = None
        self.password = None
        self.n_scrapes = 0

    def _load_credentials(self):
        """Loads Decodo credentials from the secrets manager."""
        self.username = get_secret("decodo_username")
        self.password = get_secret("decodo_password")

        if self.username and self.password:
            logger.info("✅ Decodo credentials loaded successfully.")
        else:
            logger.warning("⚠️ Decodo credentials not found. Please configure them in secrets.")

    def _has_credentials(self) -> bool:
        """Checks if Decodo credentials are available."""
        return bool(self.username and self.password)

    async def scrape(self, url: str,
                     remove_urls: bool,
                     session: aiohttp.ClientSession,
                     enable_js: bool = True,
                     timeout: int = 30) -> Optional[MultimodalSequence]:
        """Downloads the contents of the specified webpage using Decodo's API.

        Args:
            url: The URL to scrape
            remove_urls: Whether to remove URLs from hyperlinks in the result
            session: The aiohttp ClientSession to use
            enable_js: Whether to enable JavaScript rendering (default: True)
            timeout: Request timeout in seconds (default: 30)

        Returns:
            MultimodalSequence containing the scraped content, or None if scraping failed
        """
        if not self._has_credentials():
            self._load_credentials()

        if not self._has_credentials():
            logger.warning("Cannot scrape with Decodo: credentials not configured.")
            return None

        # Try with JS rendering first if enabled
        html = await self._call_decodo(url, session, enable_js, timeout)

        # If it failed with JS and we got a 400 error, try without JS
        # (400 might mean the plan doesn't support headless rendering)
        if html is None and enable_js:
            logger.debug("Retrying without JavaScript rendering...")
            html = await self._call_decodo(url, session, enable_js=False, timeout=timeout)

        if html:
            return await to_multimodal_sequence(html, remove_urls=remove_urls, session=session)
        return None

    async def _call_decodo(self, url: str,
                          session: aiohttp.ClientSession,
                          enable_js: bool = True,
                          timeout: int = 30) -> Optional[str]:
        """Calls the Decodo API to scrape the given URL.

        Args:
            url: The URL to scrape
            session: The aiohttp ClientSession to use
            enable_js: Whether to enable JavaScript rendering
            timeout: Request timeout in seconds

        Returns:
            HTML content as a string, or None if scraping failed
        """
        headers = {
            'Content-Type': 'application/json',
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

        # Create basic auth
        auth = aiohttp.BasicAuth(self.username, self.password)

        # logger.debug(f"Decodo request payload: {payload}")

        try:
            async with session.post(
                self.DECODO_API_URL,
                json=payload,
                headers=headers,
                auth=auth,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:

                if response.status != 200:
                    # Try to get the error message from response
                    try:
                        error_body = await response.json()
                        error_msg = error_body.get('message', error_body)
                    except:
                        error_msg = await response.text()

                    logger.info(
                        f"Failed to scrape {url} with Decodo\n"
                        f"Status code: {response.status} - Reason: {response.reason}\n"
                        f"Error details: {error_msg}"
                    )

                    match response.status:
                        case 401:
                            logger.error("Error 401: Unauthorized. Check your Decodo credentials.")
                        case 402:
                            logger.debug("Error 402: Payment required. Check your Decodo subscription.")
                        case 403:
                            logger.debug("Error 403: Forbidden.")
                        case 408:
                            logger.warning("Error 408: Timeout! Website did not respond in time.")
                        case 429:
                            logger.warning("Error 429: Rate limit exceeded. Too many requests.")
                        case 500:
                            logger.debug("Error 500: Server error.")
                        case _:
                            logger.debug(f"Error {response.status}: {response.reason}.")

                    return None

                # Parse response
                json_response = await response.json()

                # Extract HTML content from results
                if "results" in json_response and len(json_response["results"]) > 0:
                    result = json_response["results"][0]

                    # Check status code from the actual request
                    status_code = result.get("status_code")
                    if status_code and status_code >= 400:
                        logger.warning(f"Target website returned status {status_code} for {url}")
                        return None

                    html_content = result.get("content")
                    if html_content:
                        self.n_scrapes += 1
                        logger.debug(f"Successfully scraped {url} with Decodo (scrape #{self.n_scrapes})")
                        return html_content
                    else:
                        logger.warning(f"No content in Decodo response for {url}")
                        return None
                else:
                    logger.warning(f"No results in Decodo response for {url}")
                    logger.debug(f"Response: {json_response}")
                    return None

        except aiohttp.ClientError as e:
            logger.error(f"Network error while scraping {url} with Decodo: {repr(e)}")
            return None
        except asyncio.TimeoutError:
            logger.warning(f"Timeout while scraping {url} with Decodo. Skipping...")
            return None
        except Exception as e:
            logger.error(f"Unexpected error while scraping {url} with Decodo: {repr(e)}")
            return None


# Create a singleton instance
decodo = Decodo()
