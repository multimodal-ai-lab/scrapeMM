import asyncio
import logging
from abc import ABC, abstractmethod

from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.server.util import get_domain

logger = logging.getLogger("scrapeMM")


class RetrievalIntegration(ABC):
    """Any integration used to retrieve information via an external API. Typically
     used when direct URL scraping is not possible."""

    name: str
    domains: list[str]  # The domains supported by this integration
    connected: bool | None = None

    @property
    def _connect_lock(self) -> asyncio.Lock:
        """Guards connecting, so that concurrent retrievals and dashboard polls do not
        each build their own session.

        Created on first use rather than in `__init__`, because the subclasses that
        define one of their own do not call up to it.
        """
        lock = self.__dict__.get("_connect_lock_instance")
        if lock is None:
            lock = self.__dict__["_connect_lock_instance"] = asyncio.Lock()
        return lock

    @abstractmethod
    async def _connect(self):
        """Establish a connection to the external service. Invoked upon the first get() call.
        Must set self.connect = True if connection was successful, else False."""
        raise NotImplementedError

    async def ensure_connected(self) -> None:
        """Connects once, however many callers arrive at the same moment.

        Without this, a burst of concurrent retrievals each ran `_connect()`, which for
        session-based services means several clients racing for one session -- a locked
        SQLite file for Telegram, a rate-limited login for Bluesky.
        """
        if self.connected:
            return
        async with self._connect_lock:
            if self.connected:  # Another caller connected while we waited
                return
            await self._connect()

    async def _still_connected(self) -> bool:
        """Whether an established connection is still usable.

        Default: an established connection is assumed good. Services whose session can
        die underneath us override this with a cheap, local check -- cheap being the
        point, since the dashboard asks on every poll.
        """
        return True

    async def get(self, url: str, **kwargs) -> ScrapedContent:
        """Executes the retrieval routine. Ensures connectivity before invoking the
         retrieval. Raises an exception if anything goes wrong during retrieval."""
        assert get_domain(url) in self.domains, f"Invalid domain {get_domain(url)} for integration {self.name}."

        await self.ensure_connected()

        if not self.connected:
            raise RuntimeError(f"Connection to {self.name} service could not be established.")

        logger.debug(f"Calling {self.name} service for {url}")
        return await self._get(url, **kwargs)

    async def probe(self) -> None:
        """Checks whether this integration could be used right now, for the dashboard.

        Reuses a healthy connection rather than replacing it. Re-connecting on every
        poll is what made Telegram report "database is locked" (a second client opening
        the same session file) and Bluesky "Could not connect" (a fresh login against a
        rate-limited endpoint). A dashboard asking how things are must not change how
        things are.
        """
        if self.connected and await self._still_connected():
            return
        self.connected = None
        await self.ensure_connected()

    @abstractmethod
    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves the contents present at the given URL. Integrations having access
        to the source HTML construct the ScrapedContent with
        `scrapemm.server.util.to_scraped_content()`, all others (e.g. API-based integrations)
        wrap their MultimodalSequence via `ScrapedContent(multimodal=...)`."""
        raise NotImplementedError
