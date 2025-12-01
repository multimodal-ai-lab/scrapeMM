import asyncio
import logging
import re
import sys
from typing import Optional, Awaitable, Iterable

import tqdm

logger = logging.getLogger("scrapeMM")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/123.0.0.0 Safari/537.36",
}

DOMAIN_REGEX = r"(?:https?:\/\/)?(?:www\.)?([-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6})/?"


def get_domain(url: str, keep_subdomain: bool = False) -> Optional[str]:
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


async def run_with_semaphore(tasks: Iterable[Awaitable],
                             limit: int,
                             show_progress: bool = True,
                             progress_description: str | None = None) -> list:
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


def read_urls_from_file(file_path):
    with open(file_path, 'r') as f:
        return f.read().splitlines()


def get_multiline_user_input(prompt: str) -> str:
    print(prompt)
    lines = sys.stdin.readlines()
    return "".join(lines)
