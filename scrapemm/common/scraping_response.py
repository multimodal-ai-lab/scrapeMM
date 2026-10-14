from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from ezmm import Image, MultimodalSequence

OutputFormat = Literal["multimodal", "markdown", "html"]
OUTPUT_FORMATS = ("multimodal", "markdown", "html")


def utc_iso(when) -> Optional[str]:
    """A time -- a datetime (naive ones taken as UTC) or Unix seconds -- in ISO 8601, UTC."""
    if when is None:
        return None
    if isinstance(when, (int, float)):
        when = datetime.fromtimestamp(when, tz=timezone.utc)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass
class OmittedMedium:
    """A medium that belongs to the content but was left out of it, and why."""

    url: str  # Where the medium is: its own URL, or the page's if it has none of its own
    kind: Literal["video", "image"] = "video"
    reason: Literal["too_large"] = "too_large"  # Larger than the size limit (`max_video_size`)
    # Its size in bytes if known, beforehand or by downloading it; None if its download
    # was stopped at the limit, i.e., it is only known to be larger
    size: Optional[int] = None
    limit: Optional[int] = None  # The size limit it exceeded, in bytes

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OmittedMedium":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class Capture:
    """Where content comes from if it is an archived copy rather than the live page."""

    archive: str  # The archiving service, e.g. "Internet Archive (Wayback Machine)"
    original_url: Optional[str] = None  # The page that was archived
    captured_at: Optional[str] = None  # When it was captured, ISO 8601 (UTC)
    archive_url: Optional[str] = None  # The capture itself

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Capture":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    def note(self) -> str:
        """The capture's facts as Markdown, to stand before the archived content."""
        when = ""
        if self.captured_at:
            try:
                when = f" on {datetime.fromisoformat(self.captured_at).strftime('%Y-%m-%d %H:%M UTC')}"
            except ValueError:
                when = f" on {self.captured_at}"
        lines = [f"> **Archived copy**: not the live page, but its capture by {self.archive}{when}."]
        if self.original_url:
            lines.append(f"> Original URL: {self.original_url}")
        if self.archive_url and self.archive_url != self.original_url:
            lines.append(f"> Capture: {self.archive_url}")
        return "\n".join(lines) + "\n\n"


@dataclass
class ScrapedContent:
    """The content retrieved by a single scraping method, in each of the formats
    the method was able to produce."""

    html: Optional[str] = None  # The raw HTML code of the scraped page
    markdown: Optional[str] = None  # The scraped text in Markdown format. Media is referenced by hyperlink.
    multimodal: Optional[MultimodalSequence] = None  # The scraped text with the media downloaded and embedded
    stripped: bool = False  # Whether UI elements (navigation, footers, banners, etc.) were removed from all formats
    # The media left out of the content, e.g. videos over the size limit
    omitted_media: list[OmittedMedium] = field(default_factory=list)
    # Set if the content is an archive's capture of the page (see `Capture`)
    capture: Optional[Capture] = None
    # When the post, upload or article was published, as far as the source says: ISO 8601,
    # UTC (e.g. "2026-03-15T08:47:41+00:00"), or less precise if the source is ("2013")
    published_at: Optional[str] = None

    def __bool__(self) -> bool:
        """True iff any content is available."""
        return any(c is not None for c in (self.html, self.markdown, self.multimodal))

    def get(self, output_format: OutputFormat) -> Optional[MultimodalSequence | str]:
        """Returns the content in the requested format (None if unavailable)."""
        assert output_format in OUTPUT_FORMATS, f"Unknown output format '{output_format}'."
        return getattr(self, output_format)

    def prefixed(self, text: str) -> "ScrapedContent":
        """A copy with `text` before the Markdown and the multimodal text; the HTML stays."""
        multimodal = self.multimodal
        if multimodal is not None:
            parts = list(multimodal)
            if parts and isinstance(parts[0], str):
                parts[0] = text + parts[0]
            else:
                parts.insert(0, text)
            multimodal = MultimodalSequence(parts)
        return replace(self, markdown=text + self.markdown if self.markdown is not None else None,
                       multimodal=multimodal)


@dataclass
class ScrapingResponse:
    url: str  # The input URL that was scraped
    content: Optional[ScrapedContent]

    # Meta
    method: Optional[str] = None  # The successful method used to retrieve the content
    output_format: OutputFormat = "multimodal"  # The format that was requested
    errors: dict[str, Optional[Exception]] | None = None  # The exceptions raised during retrieval for each method
    # Seconds from the URL's first outgoing request to its result (see server `timing`)
    retrieval_time: Optional[float] = None
    # Seconds the URL waited before that, for a concurrency or browser slot
    queue_time: Optional[float] = None
    from_cache: bool = False  # Whether this response was served from the cache
    screenshot: Optional[Image] = None  # The page as the server's browser shows it, if requested

    @property
    def success(self) -> bool:
        """Whether the scraping was successful, i.e., any content was retrieved."""
        if self.content:
            match self.output_format:
                case "html":
                    return self.content.html is not None
                case "markdown":
                    return self.content.markdown is not None
                case "multimodal":
                    return self.content.multimodal is not None
                case _:
                    raise ValueError(f"Unknown output format '{self.output_format}'.")
        return False

    @property
    def published_at(self) -> Optional[str]:
        """When the content was published, if the source says (see `ScrapedContent`)."""
        return self.content.published_at if self.content else None

    @property
    def capture(self) -> Optional[Capture]:
        """The archive capture the content comes from, if it is one."""
        return self.content.capture if self.content else None

    @property
    def omitted_media(self) -> list[OmittedMedium]:
        """The media left out of the content, e.g. videos over `max_video_size`."""
        return self.content.omitted_media if self.content else []

    def get(self) -> Optional[MultimodalSequence | str]:
        """Returns the retrieved content in the requested format (None if unavailable)."""
        if self.content:
            return self.content.get(self.output_format)
        return None
