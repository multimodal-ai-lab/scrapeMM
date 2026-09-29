"""`/v1/search` -- web search through third-party search APIs.

Search APIs have little in common, so each provider has a route of its own, POST
/v1/search/<provider>. It takes that provider's query class (from `scrapemm.search`)
as its body and answers with the provider's response, in the provider's own format.
One search is one short call, so the answer is a plain JSON object, not a stream.

Searches are not recorded as jobs: the job history is about URLs retrieved, and a
search retrieves none.

Every failure carries a string `detail`, for the UI, and the exception it stands for
in `error`, from which the client rebuilds it -- a rate limit arrives in the caller's
code as the RateLimitError it is.
"""

# No `from __future__ import annotations` here: each provider route is annotated with
# its query class at runtime, and FastAPI needs the class, not its name as a string.

import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from scrapemm.common import QuotaExceededError, RateLimitError
from scrapemm.common.exceptions import exception_to_wire
from scrapemm.common.paths import APP_NAME
from ..auth import require_api_key
from ..search import (SEARCH_PROVIDERS, ProviderDisabled, ProviderNotConfigured,
                      SearchProvider)

logger = logging.getLogger(APP_NAME)

router = APIRouter(prefix="/v1/search", tags=["search"],
                   dependencies=[Depends(require_api_key)])

# First match wins, so a subclass must come before the class it derives from
# (ProviderNotConfigured and ProviderDisabled are RuntimeErrors). Never 401: the UI
# takes a 401 to mean its own key was rejected and signs out -- a provider refusing
# *its* key is a 502.
ERROR_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (ProviderNotConfigured, 503),
    (ProviderDisabled, 503),
    (RateLimitError, 429),
    (QuotaExceededError, 402),
    (TimeoutError, 504),
    (ValueError, 400),
    (RuntimeError, 502),
)


@router.get("")
async def list_providers() -> dict:
    return {"providers": [provider.describe() for provider in SEARCH_PROVIDERS]}


def _endpoint(provider: SearchProvider):
    async def run_search(query: provider.query_class) -> Any:
        try:
            response = await provider.search(query)
        except Exception as e:
            return _failure(provider, e)
        return response.to_dict()

    return run_search


for _provider in SEARCH_PROVIDERS:
    router.add_api_route(
        f"/{_provider.name}", _endpoint(_provider), methods=["POST"],
        name=f"search_{_provider.name}", summary=f"Search with {_provider.label}",
        description=f"Runs a {_provider.label} search ({_provider.homepage}). The body "
                    f"is a `{_provider.query_class.__name__}`, the answer "
                    f"{_provider.label}'s response in {_provider.label}'s own format.")


# After the provider routes, so that it only ever sees names none of them took
@router.post("/{provider}", include_in_schema=False)
async def unknown_provider(provider: str) -> JSONResponse:
    available = ", ".join(p.name for p in SEARCH_PROVIDERS)
    return _error(404, ValueError(f"Unknown search provider '{provider}'. Available: "
                                  f"{available}."))


def _failure(provider: SearchProvider, e: Exception) -> JSONResponse:
    status = next((code for cls, code in ERROR_STATUS if isinstance(e, cls)), 500)
    if status == 500:
        logger.error(f"{provider.label} search failed unexpectedly.", exc_info=e)
    elif status > 500:
        logger.warning(f"{provider.label} search failed: {e}")
    else:  # The caller's doing, or the caller's quota: nothing wrong with the server
        logger.info(f"{provider.label} search refused: {e}")
    return _error(status, e)


def _error(status: int, e: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(e), "error": exception_to_wire(e)},
                        status_code=status)
