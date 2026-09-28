"""Web search through a scrapeMM server.

Every search API has its own parameters and its own results, so each provider scrapeMM
supports comes with its own query and response classes, mirroring that provider's API.
Pick a provider by building its query:

    import asyncio
    from scrapemm.search import SerperQuery, search

    response = asyncio.run(search(SerperQuery(q="eiffel tower")))
    response.organic[0].title        # Serper's own fields, typed
    response.urls                    # every provider's: the result pages, for retrieve()

The provider's API key is configured on the server, under Secrets in its web UI.

Adding a provider takes four steps:
1. Its query and response classes, in `scrapemm/search/<provider>.py`, subclassing
   `SearchQuery` and `SearchResponse`.
2. The server side that calls the provider's API, in
   `scrapemm/server/search/<provider>.py`, subclassing `SearchProvider`.
3. An instance of that in `SEARCH_PROVIDERS` (`scrapemm/server/search/__init__.py`),
   which also gives it its route, POST /v1/search/<provider>.
4. Its API key in `SECRETS` (`scrapemm/server/secrets.py`).
"""

from .base import SearchQuery, SearchResponse, WireObject, WireRecord
from .client import search
from .serper import (SERPER_TYPES, SerperAnswerBox, SerperImage, SerperKnowledgeGraph,
                     SerperOrganicResult, SerperPeopleAlsoAsk, SerperQuery,
                     SerperRelatedSearch, SerperResponse, SerperSearchParameters,
                     SerperSitelink, SerperTopStory, SerperType)

__all__ = [
    "search",
    "SearchQuery",
    "SearchResponse",
    "WireObject",
    "WireRecord",
    "SerperQuery",
    "SerperResponse",
    "SerperType",
    "SERPER_TYPES",
    "SerperAnswerBox",
    "SerperImage",
    "SerperKnowledgeGraph",
    "SerperOrganicResult",
    "SerperPeopleAlsoAsk",
    "SerperRelatedSearch",
    "SerperSearchParameters",
    "SerperSitelink",
    "SerperTopStory",
]
