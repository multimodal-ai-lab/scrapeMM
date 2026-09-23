"""The client/server contract: the shapes that travel between them.

These need no server dependencies at all -- they are the client's half of the deal,
and they run wherever `pip install scrapeMM` works.
"""

from scrapemm.common.exceptions import (CaptchaEncounteredError, RetrievalFailed,
                                        ServerError, exception_from_wire,
                                        exception_to_wire)
from scrapemm.common.wire import (ContentPayload, ItemDescriptor, RegistryInfo,
                                  ResponsePayload, errors_to_wire)


def test_known_exceptions_survive_the_trip():
    """Client code catches `CaptchaEncounteredError`; it must still be able to."""
    original = CaptchaEncounteredError("a Cloudflare challenge")
    restored = exception_from_wire(exception_to_wire(original))
    assert isinstance(restored, CaptchaEncounteredError)
    assert str(restored) == "a Cloudflare challenge"


def test_unknown_exceptions_arrive_catchable():
    """An exception class the client does not know must not crash the client, and must
    not vanish either: the original name is kept in the message."""
    restored = exception_from_wire({"type": "SomeVendorError", "message": "boom"})
    assert isinstance(restored, RetrievalFailed)
    assert "SomeVendorError" in str(restored)
    assert "boom" in str(restored)


def test_subclass_is_not_flattened_to_its_parent():
    """CaptchaEncounteredError subclasses AccessBlockedError; the more specific class
    is the one worth keeping."""
    restored = exception_from_wire(exception_to_wire(CaptchaEncounteredError("x")))
    assert type(restored) is CaptchaEncounteredError


def test_errors_to_wire_tolerates_none_entries():
    """The engine files a `None` for a method that failed without an exception."""
    wire = errors_to_wire({"firecrawl": None, "decodo": RetrievalFailed("no")})
    assert set(wire) == {"firecrawl", "decodo"}
    assert wire["decodo"]["type"] == "RetrievalFailed"


def test_errors_to_wire_handles_empty():
    assert errors_to_wire(None) == {}
    assert errors_to_wire({}) == {}


def test_response_payload_round_trip():
    payload = ResponsePayload(
        url="https://example.com",
        content=ContentPayload(
            html="<p>x</p>", markdown="x", multimodal="x <image:3>",
            items=[ItemDescriptor(ref="<image:3>", kind="image", id=3,
                                  path="/data/media/image/3.png",
                                  media_url="/v1/media/image/3", size=17)]),
        method="firecrawl",
        output_format="multimodal",
        errors={"decodo": {"type": "RetrievalFailed", "message": "no"}},
        retrieval_time=1.5,
        from_cache=True,
    )
    restored = ResponsePayload.from_dict(payload.to_dict())
    assert restored.url == payload.url
    assert restored.content.items[0].path == "/data/media/image/3.png"
    assert restored.method == "firecrawl"
    assert restored.from_cache is True


def test_to_response_rebuilds_a_scraping_response():
    payload = ResponsePayload(
        url="https://example.com",
        content=ContentPayload(markdown="hello"),
        output_format="markdown",
        errors={"decodo": {"type": "CaptchaEncounteredError", "message": "gate"}},
    )
    response = payload.to_response()
    assert response.success is True
    assert response.get() == "hello"
    assert isinstance(response.errors["decodo"], CaptchaEncounteredError)


def test_to_response_without_content_is_a_failure():
    payload = ResponsePayload(url="https://example.com", output_format="markdown")
    response = payload.to_response()
    assert response.success is False
    assert response.get() is None


def test_empty_content_does_not_masquerade_as_content():
    """A payload whose every format is None must not produce a truthy ScrapedContent,
    or `success` would start lying."""
    payload = ResponsePayload(url="https://example.com",
                              content=ContentPayload(), output_format="markdown")
    assert payload.to_response().content is None


def test_registry_info_defaults_are_safe():
    """A server that exposes nothing must not be mistaken for one that does."""
    info = RegistryInfo.from_dict({})
    assert info.root is None
    assert info.fingerprint is None
    assert info.writable_by_client is False


def test_server_error_is_transportable():
    restored = exception_from_wire(exception_to_wire(ServerError("down")))
    assert isinstance(restored, ServerError)
