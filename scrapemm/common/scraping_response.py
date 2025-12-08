from dataclasses import dataclass
from typing import Optional

from ezmm import MultimodalSequence


@dataclass
class ScrapingResponse:
    url: str  # The input URL that was scraped
    content: Optional[MultimodalSequence | str]  # The retrieved content. None if retrieval failed.
    errors: dict[str, Optional[Exception]] | None = None  # The exceptions raised during retrieval for each method
    method: Optional[str] = None  # The successful method used to retrieve the content

    @property
    def successful(self):
        return self.content is not None
