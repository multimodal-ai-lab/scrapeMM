"""ResearchGate publications, by way of OpenAlex.

ResearchGate puts every visitor from this server behind DataDome's bot check, which no
method gets past. Its publication URLs name the paper, though
(/publication/339908838_The_benefit_of_Indian_jaggery_over_sugar_on_human_health), and
OpenAlex, an open catalogue of scholarly works, knows the papers: title, authors, date,
venue, DOI, mostly the abstract. The response says plainly that the record is OpenAlex's,
of the paper the ResearchGate page shows, not that page itself."""

import difflib
import logging
import re
from typing import Optional
from urllib.parse import unquote, urlparse

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.common.exceptions import AccessBlockedError, RateLimitError
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.server.integrations.base import RetrievalIntegration
from scrapemm.server.published import normalize

logger = logging.getLogger("scrapeMM")

OPENALEX_SEARCH = "https://api.openalex.org/works"
# OpenAlex's "polite pool": faster and steadier for clients that say who they are
USER_AGENT = "scrapeMM (+https://github.com/multimodal-ai-lab/scrapeMM)"
PUBLICATION_PATH = re.compile(r"^/publication/(\d+)_([^/?#]+)")
MIN_TITLE_SIMILARITY = 0.9  # Of the URL's title and OpenAlex's, normalized
MAX_AUTHORS = 20


class ResearchGate(RetrievalIntegration):
    name = "ResearchGate"
    domains = ["researchgate.net"]

    async def _connect(self):
        self.connected = True  # OpenAlex needs no account

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        match = PUBLICATION_PATH.match(urlparse(url).path)
        if not match:
            raise AccessBlockedError(f"ResearchGate turns this server away (DataDome), and {url} "
                                     f"names no publication to look up elsewhere.")
        title = unquote(match.group(2)).replace("_", " ").strip()
        work = await self._find(title)
        if work is None:
            raise AccessBlockedError(f"ResearchGate turns this server away (DataDome), and "
                                     f"OpenAlex knows no paper titled \"{title}\".")
        return ScrapedContent(multimodal=MultimodalSequence(_describe(work, url)),
                              published_at=normalize(work.get("publication_date") or "")
                              or (str(work["publication_year"]) if work.get("publication_year") else None))

    @staticmethod
    async def _find(title: str) -> Optional[dict]:
        """The OpenAlex work whose title is the given one, near enough; None if none is."""
        params = {"search": title, "per_page": "5"}
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(OPENALEX_SEARCH, params=params,
                                   timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status == 429:
                    raise RateLimitError("OpenAlex is rate-limiting this server.")
                response.raise_for_status()
                results = (await response.json()).get("results") or []
        scored = [(_similarity(title, work.get("title") or ""), work) for work in results]
        best = max(scored, key=lambda pair: pair[0], default=(0.0, None))
        if best[0] < MIN_TITLE_SIMILARITY:
            logger.info(f"OpenAlex has no paper titled \"{title}\" (best match: {best[0]:.2f}).")
            return None
        return best[1]


def _similarity(a: str, b: str) -> float:
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def _abstract(work: dict) -> Optional[str]:
    """The abstract, which OpenAlex keeps as an inverted index (word -> positions)."""
    index = work.get("abstract_inverted_index")
    if not index:
        return None
    positions = sorted((position, word) for word, places in index.items() for position in places)
    return " ".join(word for _, word in positions)


def _describe(work: dict, url: str) -> str:
    authors = [a.get("author", {}).get("display_name") for a in work.get("authorships") or []]
    authors = [a for a in authors if a]
    more = f", and {len(authors) - MAX_AUTHORS} more" if len(authors) > MAX_AUTHORS else ""
    location = work.get("primary_location") or {}
    venue = (location.get("source") or {}).get("display_name")
    open_access = (work.get("open_access") or {}).get("oa_url")
    lines = [
        "**Publication on ResearchGate**",
        "(ResearchGate turns this server away; this is the paper's record in OpenAlex, "
        f"an open catalogue of scholarly works: {work.get('id')})",
        f"Title: {work.get('title')}",
        f"Authors: {', '.join(authors[:MAX_AUTHORS])}{more}" if authors else None,
        f"Published: {work.get('publication_date') or work.get('publication_year')}",
        f"Venue: {venue}" if venue else None,
        f"DOI: {work.get('doi')}" if work.get("doi") else None,
        f"Open access copy: {open_access}" if open_access else None,
        f"Type: {work.get('type')}" if work.get("type") else None,
        f"Cited by: {work['cited_by_count']}" if work.get("cited_by_count") is not None else None,
        f"ResearchGate page: {url}",
    ]
    text = "\n".join(line for line in lines if line)
    if abstract := _abstract(work):
        text += f"\n\nAbstract:\n{abstract}"
    return text
