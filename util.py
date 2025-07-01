import asyncio
import logging
import re
import sys
from typing import Optional, Awaitable, Iterable

import aiohttp
import tqdm
from pydantic import HttpUrl

logger = logging.getLogger("Retriever")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/123.0.0.0 Safari/537.36",
}

DOMAIN_REGEX = r"(?:https?:\/\/)?(?:www\.)?([-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6})/?"


def get_domain(url: str | HttpUrl, keep_subdomain: bool = False) -> Optional[str]:
    """Uses regex to get out the domain from the given URL. The output will be
    of the form 'example.com'. No 'www', no 'http'."""
    url = str(url)
    match = re.search(DOMAIN_REGEX, url)
    if match:
        domain = match.group(1)
        if not keep_subdomain:
            # Keep only second-level and top-level domain
            domain = '.'.join(domain.split('.')[-2:])
        return domain


async def fetch_headers(url, session: aiohttp.ClientSession, **kwargs) -> dict:
    async with session.head(url, **kwargs) as response:
        response.raise_for_status()
        return dict(response.headers)


async def request_static(url: str | HttpUrl,
                         session: aiohttp.ClientSession,
                         get_text: bool = True,
                         **kwargs) -> Optional[str | bytes]:
    """Downloads the static page from the given URL using aiohttp. If `get_text` is True,
    returns the HTML as text. Otherwise, returns the raw binary content (e.g. an image)."""
    # TODO: Handle web archive URLs
    if url:
        url = str(url)
        try:
            async with session.get(url, timeout=10, headers=HEADERS, allow_redirects=True,
                                   raise_for_status=True, **kwargs) as response:
                if get_text:
                    return await response.text()  # HTML string
                else:
                    return await stream(response)  # Binary data
        except asyncio.TimeoutError:
            pass  # Server too slow
        except UnicodeError:
            pass  # Page not readable
        except (aiohttp.ClientOSError, aiohttp.ClientConnectorError):
            pass  # Page not available anymore
        except aiohttp.ClientResponseError as e:
            if e.status in [403, 404, 429, 500, 502, 503]:
                # 403: Forbidden access
                # 404: Not found
                # 429: Too many requests
                # 500: Server error
                # 502: Bad gateway
                # 503: Service unavailable (e.g. rate limit)
                pass
            else:
                logger.debug(f"\rFailed to retrieve page.\n\t{type(e).__name__}: {e}")
        except Exception as e:
            logger.debug(f"\rFailed to retrieve page at {url}.\n\tReason: {type(e).__name__}: {e}")


async def stream(response: aiohttp.ClientResponse, chunk_size: int = 1024) -> bytes:
    data = bytearray()
    async for chunk in response.content.iter_chunked(chunk_size):
        data.extend(chunk)
    return bytes(data)  # Convert to immutable bytes if needed


async def run_with_semaphore(tasks: Iterable[Awaitable],
                             limit: int,
                             show_progress: bool = True,
                             progress_description: str = None) -> list:
    """
    Runs asynchronous tasks with a concurrency limit.

    Args:
        tasks: The tasks to execute concurrently.
        limit: The maximum number of coroutines to run concurrently.
        show_progress: Whether to show a progress bar while executing tasks.
        progress_description: The message to display in the progress bar.

    Returns:
        list: A list of results returned by the tasks, order-preserved.
    """
    semaphore = asyncio.Semaphore(limit)  # Limit concurrent executions

    async def limited_coroutine(t: Awaitable):
        async with semaphore:
            return await t

    print(progress_description, end="\r")

    tasks = [asyncio.create_task(limited_coroutine(task)) for task in tasks]

    # Report completion status of tasks (if more than one task)
    if show_progress:
        progress = tqdm.tqdm(total=len(tasks), desc=progress_description, file=sys.stdout)
        while progress.n < len(tasks):
            progress.n = sum(task.done() for task in tasks)
            progress.refresh()
            await asyncio.sleep(0.1)
        progress.close()

    return await asyncio.gather(*tasks)
