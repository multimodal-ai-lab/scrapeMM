"""`scrapemm.search.search()` against a fake scrapeMM server, over real HTTP.

Needs nothing but the client: the server here is a few aiohttp handlers that answer
the way the real one does.
"""

import json
import socket
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from scrapemm.client.settings import Settings
from scrapemm.common import QuotaExceededError, RateLimitError, ServerError
from scrapemm.search import SerperQuery, SerperResponse, search

SAMPLE = json.loads((Path(__file__).parent / "data" / "serper_search.json")
                    .read_text(encoding="utf-8"))


@pytest.fixture
async def serve():
    """Starts a fake server with the given routes and returns client settings that
    point at it."""
    servers = []

    async def start(routes: dict) -> Settings:
        app = web.Application()
        for (method, path), handler in routes.items():
            app.router.add_route(method, path, handler)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return Settings(api_url=str(server.make_url("")).rstrip("/"), api_key="k")

    yield start
    for server in servers:
        await server.close()


def failing(status: int, error_type: str, message: str):
    """A handler that fails the way the search route does."""
    async def handler(request):
        return web.json_response({"detail": message,
                                  "error": {"type": error_type, "message": message}},
                                 status=status)
    return handler


async def test_the_answer_is_the_providers_response(serve):
    seen = {}

    async def handler(request):
        seen["body"] = await request.json()
        seen["authorization"] = request.headers.get("Authorization")
        return web.json_response(SAMPLE)

    config = await serve({("POST", "/v1/search/serper"): handler})
    response = await search(SerperQuery(q="apple inc", num=10), config=config)

    assert isinstance(response, SerperResponse)
    assert response.organic[0].link == "https://www.apple.com/"
    assert response.to_dict() == SAMPLE
    assert seen["body"] == {"q": "apple inc", "type": "search", "num": 10}
    assert seen["authorization"] == "Bearer k"


async def test_a_list_of_queries_gets_a_list_of_responses_in_order(serve):
    async def handler(request):
        query = (await request.json())["q"]
        return web.json_response({**SAMPLE, "searchParameters": {"q": query}})

    config = await serve({("POST", "/v1/search/serper"): handler})
    responses = await search([SerperQuery(q="a"), SerperQuery(q="b"),
                              SerperQuery(q="c")], config=config)

    assert [r.search_parameters.q for r in responses] == ["a", "b", "c"]
    assert all(isinstance(r, SerperResponse) for r in responses)


async def test_an_empty_list_of_queries_gets_an_empty_list():
    config = Settings(api_url="http://127.0.0.1:9", api_key="k")  # Never contacted
    assert await search([], config=config) == []


@pytest.mark.parametrize("status, error_type, expected", [
    (429, "RateLimitError", RateLimitError),
    (402, "QuotaExceededError", QuotaExceededError),
    (400, "ValueError", ValueError),
    (404, "ValueError", ValueError),
    (504, "TimeoutError", TimeoutError),
])
async def test_failures_arrive_as_the_exceptions_they_are(serve, status, error_type,
                                                          expected):
    config = await serve({("POST", "/v1/search/serper"):
                          failing(status, error_type, "the reason")})
    with pytest.raises(expected, match="the reason"):
        await search(SerperQuery(q="x"), config=config)


@pytest.mark.parametrize("status, error_type", [(502, "RuntimeError"),
                                                (503, "ProviderNotConfigured"),
                                                (500, "KeyError")])
async def test_the_servers_own_trouble_is_a_server_error(serve, status, error_type):
    config = await serve({("POST", "/v1/search/serper"):
                          failing(status, error_type, "set serper_api_key")})
    with pytest.raises(ServerError, match="set serper_api_key"):
        await search(SerperQuery(q="x"), config=config)


async def test_a_rejected_api_key_says_how_to_set_it(serve):
    async def handler(request):
        return web.json_response({"detail": "Invalid or missing API key."}, status=401)

    config = await serve({("POST", "/v1/search/serper"): handler})
    with pytest.raises(ServerError, match=r"configure\(api_key=\.\.\.\)"):
        await search(SerperQuery(q="x"), config=config)


async def test_a_server_without_search_is_told_apart(serve):
    """An older scrapeMM server answers 404 with no `error`: the fix is to update it,
    not to change the URL."""
    async def healthz(request):
        return web.json_response({"status": "ok", "version": "1.0.0"})

    config = await serve({("GET", "/healthz"): healthz})
    with pytest.raises(ServerError, match="has no POST /v1/search"):
        await search(SerperQuery(q="x"), config=config)


async def test_an_unreachable_server_is_a_server_error():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = Settings(api_url=f"http://127.0.0.1:{port}", api_key="k")
    with pytest.raises(ServerError, match="Could not reach"):
        await search(SerperQuery(q="x"), config=config)


async def test_an_invalid_query_is_not_sent():
    config = Settings(api_url="http://127.0.0.1:9", api_key="k")  # Never contacted
    with pytest.raises(ValueError, match="must not be empty"):
        await search(SerperQuery(q=""), config=config)
    with pytest.raises(ValueError, match="must not be empty"):
        await search([SerperQuery(q="x"), SerperQuery(q="")], config=config)
