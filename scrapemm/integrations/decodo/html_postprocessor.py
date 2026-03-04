from abc import ABC, abstractmethod

from bs4 import BeautifulSoup

from scrapemm.integrations.archive_today import ARCHIVE_TODAY_CONTENT_DIV_ID


class HtmlPostProcessor(ABC):
    """
    Abstract base class for HTML post-processors used after retrieval with Decodo.
    HTML post-processors are responsible for processing raw HTML content retrieved
    from certain sources (e.g., Archive.today) into a desired format.

    Example:
        Archive.today wraps archived content in a specific div, and the HTML post-processor
        can be used to extract the inner HTML of that div.
    """

    @staticmethod
    @abstractmethod
    def process(html: str) -> str:
        pass


class ArchiveTodayPostProcessor(HtmlPostProcessor):
    """
    HTML post-processor for content retrieved from Archive.today using Decodo.
    Extracts the inner HTML of the div with id 'content' which contains the archived page content.
    """

    @staticmethod
    def process(html: str) -> str:
        """
        Try to extract the record content from the HTML retrieved from Archive.today. 
        If the expected div is not found, return the original HTML.
        """
        soup = BeautifulSoup(html, "html.parser")
        content_div = soup.find("div", id=ARCHIVE_TODAY_CONTENT_DIV_ID)
        return content_div.decode_contents() if content_div else html


domain_to_postprocessor = {
    "archive.today": ArchiveTodayPostProcessor,
    "archive.is": ArchiveTodayPostProcessor,
    "archive.ph": ArchiveTodayPostProcessor,
    "archive.vn": ArchiveTodayPostProcessor,
    "archive.li": ArchiveTodayPostProcessor,
    "archive.fo": ArchiveTodayPostProcessor,
    "archive.md": ArchiveTodayPostProcessor,
}
