"""Archived copies say so before their content: which archive, when, and of which page."""

import pytest
from ezmm import MultimodalSequence

from scrapemm.common.scraping_response import Capture, ScrapedContent, ScrapingResponse
from scrapemm.common.wire import ContentPayload, ResponsePayload

pytestmark = pytest.mark.server

CAPTURE = Capture(archive="the Internet Archive (Wayback Machine)",
                  original_url="https://example.com/a",
                  captured_at="2026-01-23T14:49:19+00:00",
                  archive_url="https://web.archive.org/web/20260123144919/https://example.com/a")


def test_the_note_names_the_archive_the_time_and_the_page():
    note = CAPTURE.note()
    assert note.startswith("> **Archived copy**: not the live page, but its capture by the Internet "
                           "Archive (Wayback Machine) on 2026-01-23 14:49 UTC.\n")
    assert "> Original URL: https://example.com/a\n" in note
    assert "> Capture: https://web.archive.org/web/20260123144919/https://example.com/a\n" in note
    assert Capture(archive="Ghostarchive").note() == (
        "> **Archived copy**: not the live page, but its capture by Ghostarchive.\n\n")


def test_the_note_stands_before_the_text_but_not_in_the_cached_content():
    from scrapemm.server.engine import _with_capture_note
    content = ScrapedContent(html="<p>Text</p>", markdown="Text",
                             multimodal=MultimodalSequence(["Text"]), capture=CAPTURE)
    response = _with_capture_note(ScrapingResponse(url="u", content=content))
    assert response.content.markdown.startswith("> **Archived copy**") and \
        response.content.markdown.endswith("\n\nText")
    assert str(response.content.multimodal).startswith("> **Archived copy**")
    assert response.content.html == "<p>Text</p>"  # As captured
    assert content.markdown == "Text"  # The cached content stays as retrieved
    live = ScrapingResponse(url="u", content=ScrapedContent(markdown="Text"))
    assert _with_capture_note(live) is live


def test_the_capture_travels_to_the_client():
    payload = ResponsePayload(url="u", content=ContentPayload(markdown="m", capture=CAPTURE))
    assert ResponsePayload.from_dict(payload.to_dict()).to_response().capture == CAPTURE


def test_capture_times():
    from scrapemm.server.integrations.archive_org import wayback_time
    from scrapemm.server.integrations.megalodon import _megalodon_time
    assert wayback_time("20260123144919") == "2026-01-23T14:49:19+00:00"
    assert wayback_time("2026") is None  # Partial: the nearest capture is served
    assert _megalodon_time("2026-0123-1449-19") == "2026-01-23T05:49:19+00:00"  # From Japan time
