"""PDFs come with the file itself; a scan without a text layer with its page scans; and a
PDF that Decodo hands on base64-encoded is read like any other."""

import base64
from io import BytesIO

import pytest
from PIL import Image as PILImage, ImageDraw

from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.server.download.documents import to_content
from scrapemm.server.integrations.decodo.decodo import _base64_document
from scrapemm.server.wall_detect import raw_document

pytestmark = pytest.mark.server
pytest.importorskip("pypdf")


def scanned_pdf(pages: int = 3) -> bytes:
    """A PDF whose pages are images only, as a scanner makes them."""
    scans = []
    for number in range(pages):
        scan = PILImage.new("L", (600, 800), 255)
        ImageDraw.Draw(scan).text((50, 50), f"Page {number + 1}", fill=0)
        scans.append(scan)
    buffer = BytesIO()
    scans[0].save(buffer, format="PDF", save_all=True, append_images=scans[1:])
    return buffer.getvalue()


def test_a_scan_comes_as_its_page_scans_and_the_file():
    content = to_content(scanned_pdf(3), "bill.pdf", "application/pdf", "https://x.test/bill.pdf")
    kinds = [type(item).__name__ for item in content.multimodal if not isinstance(item, str)]
    assert kinds == ["Image", "Image", "Image", "File"]
    assert "A scanned PDF without a text layer" in content.markdown and "its 3 pages" in content.markdown


def test_a_scan_shows_ten_pages_at_most():
    content = to_content(scanned_pdf(12), "long.pdf", "application/pdf")
    assert sum(type(i).__name__ == "Image" for i in content.multimodal) == 10
    assert "the first 10 of its 12 pages" in content.markdown


def test_a_base64_pdf_is_recognised_and_decoded():
    data = scanned_pdf(1)
    encoded = base64.b64encode(data).decode()
    assert raw_document(ScrapedContent(markdown=encoded))  # Never content as it is
    assert _base64_document(encoded) == data
    assert _base64_document("<html>A page</html>") is None
