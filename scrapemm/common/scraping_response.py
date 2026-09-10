from dataclasses import dataclass
from typing import Literal, Optional

from ezmm import MultimodalSequence

OutputFormat = Literal["multimodal", "markdown", "html"]
OUTPUT_FORMATS = ("multimodal", "markdown", "html")


@dataclass
class ScrapedContent:
    """The content retrieved by a single scraping method, in each of the formats
    the method was able to produce."""

    html: Optional[str] = None  # The raw HTML code of the scraped page
    markdown: Optional[str] = None  # The scraped text in Markdown format. Media is referenced by hyperlink.
    multimodal: Optional[MultimodalSequence] = None  # The scraped text with the media downloaded and embedded

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
    retrieval_time: Optional[float] = None  # Time in seconds for retrieving this URL
    from_cache: bool = False  # Whether this response was served from the cache

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

    def get(self) -> Optional[MultimodalSequence | str]:
        """Returns the retrieved content in the requested format (None if unavailable)."""
        if self.content:
            return self.content.get(self.output_format)
        return None
