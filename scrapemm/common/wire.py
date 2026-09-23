"""The shapes that travel between the scrapeMM client and server.

Kept as plain dataclasses with explicit `to_dict`/`from_dict` so that both sides agree
on the format without the client having to depend on a validation library.

The one shape that needs explaining is the media manifest. A `MultimodalSequence`
cannot be sent as such: its items are files in the server's ezMM registry, referenced
by ids that only mean something relative to that registry. So the sequence travels as
its rendered text (`"caption <image:3> more text"`) plus one `ItemDescriptor` per item,
carrying everything the client needs to resolve the reference locally -- including the
file's *host-visible* path, which is what lets a client on the same machine adopt the
file instead of downloading a second copy of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .exceptions import exception_from_wire, exception_to_wire
from .scraping_response import (OutputFormat, ScrapedContent, ScrapingResponse)

# Bumped whenever the shapes below change incompatibly. The client refuses a server
# that speaks a different major protocol rather than failing in some subtler way later.
PROTOCOL_VERSION = 1


@dataclass
class ItemDescriptor:
    """One media file belonging to a retrieved page."""

    ref: str  # The reference as it appears in the rendered text, e.g. "<image:3>"
    kind: str  # "image", "video", ...
    id: int  # The item's id in the *server's* registry
    path: Optional[str] = None  # Host-visible absolute path, None if not exposed
    media_url: Optional[str] = None  # API path to download the bytes from
    source_url: Optional[str] = None  # Where the file was originally retrieved from
    sha256: Optional[str] = None
    size: Optional[int] = None  # File size in bytes

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ItemDescriptor":
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})


@dataclass
class RegistryInfo:
    """Identifies the server's ezMM registry, so a client can tell whether it is
    looking at the very same files (see `scrapemm.client.media`)."""

    root: Optional[str] = None  # Host-visible absolute path of the registry root
    fingerprint: Optional[str] = None  # Random id stored inside the registry root
    writable_by_client: bool = False  # Whether clients may register items by path

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RegistryInfo":
        return cls(**{k: data.get(k, getattr(cls, k, None))
                      for k in cls.__dataclass_fields__})


@dataclass
class ContentPayload:
    """`ScrapedContent` in transportable form."""

    html: Optional[str] = None
    markdown: Optional[str] = None
    multimodal: Optional[str] = None  # Rendered sequence, media by reference
    items: list[ItemDescriptor] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "html": self.html,
            "markdown": self.markdown,
            "multimodal": self.multimodal,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContentPayload":
        return cls(
            html=data.get("html"),
            markdown=data.get("markdown"),
            multimodal=data.get("multimodal"),
            items=[ItemDescriptor.from_dict(d) for d in data.get("items") or []],
        )


@dataclass
class ResponsePayload:
    """`ScrapingResponse` in transportable form."""

    url: str
    content: Optional[ContentPayload] = None
    method: Optional[str] = None
    output_format: OutputFormat = "multimodal"
    errors: dict[str, dict[str, str]] = field(default_factory=dict)
    retrieval_time: Optional[float] = None
    from_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "content": self.content.to_dict() if self.content else None,
            "method": self.method,
            "output_format": self.output_format,
            "errors": self.errors,
            "retrieval_time": self.retrieval_time,
            "from_cache": self.from_cache,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResponsePayload":
        content = data.get("content")
        return cls(
            url=data["url"],
            content=ContentPayload.from_dict(content) if content else None,
            method=data.get("method"),
            output_format=data.get("output_format", "multimodal"),
            errors=data.get("errors") or {},
            retrieval_time=data.get("retrieval_time"),
            from_cache=bool(data.get("from_cache")),
        )

    def to_response(self, multimodal=None) -> ScrapingResponse:
        """Rebuilds the `ScrapingResponse`. `multimodal` is the sequence the client
        resolved from the item manifest; it is passed in because resolving it needs
        the filesystem and may need network, neither of which belongs in here."""
        content = None
        if self.content is not None:
            content = ScrapedContent(
                html=self.content.html,
                markdown=self.content.markdown,
                multimodal=multimodal,
            )
            if not content:
                content = None
        return ScrapingResponse(
            url=self.url,
            content=content,
            method=self.method,
            output_format=self.output_format,
            errors={k: exception_from_wire(v) for k, v in self.errors.items()} or None,
            retrieval_time=self.retrieval_time,
            from_cache=self.from_cache,
        )


def errors_to_wire(errors: Optional[dict]) -> dict[str, dict[str, str]]:
    """Renders a response's error dict for transport, tolerating the `None` entries
    that the engine puts there for methods that failed without an exception."""
    if not errors:
        return {}
    return {
        method: exception_to_wire(e if isinstance(e, Exception)
                                  else RuntimeError(str(e) if e else "Unknown error"))
        for method, e in errors.items()
    }
