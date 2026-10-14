from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

from ezmm import Image, MultimodalSequence

OutputFormat = Literal["multimodal", "markdown", "html"]
OUTPUT_FORMATS = ("multimodal", "markdown", "html")


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
class ScrapedContent:
    """The content retrieved by a single scraping method, in each of the formats
    the method was able to produce."""

    html: Optional[str] = None  # The raw HTML code of the scraped page
    markdown: Optional[str] = None  # The scraped text in Markdown format. Media is referenced by hyperlink.
    multimodal: Optional[MultimodalSequence] = None  # The scraped text with the media downloaded and embedded
    stripped: bool = False  # Whether UI elements (navigation, footers, banners, etc.) were removed from all formats
    # The media left out of the content, e.g. videos over the size limit
    omitted_media: list[OmittedMedium] = field(default_factory=list)

    def __bool__(self) -> bool:
        """True iff any content is available."""
        return any(c is not None for c in (self.html, self.markdown, self.multimodal))

    def get(self, output_format: OutputFormat) -> Optional[MultimodalSequence | str]:
        """Returns the content in the requested format (None if unavailable)."""
        assert output_format in OUTPUT_FORMATS, f"Unknown output format '{output_format}'."
        return getattr(self, output_format)


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
    def omitted_media(self) -> list[OmittedMedium]:
        """The media left out of the content, e.g. videos over `max_video_size`."""
        return self.content.omitted_media if self.content else []

    def get(self) -> Optional[MultimodalSequence | str]:
        """Returns the retrieved content in the requested format (None if unavailable)."""
        if self.content:
            return self.content.get(self.output_format)
        return None
