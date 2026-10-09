from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.server.engine import _soft_not_found


def test_not_found_page_served_with_status_200_is_detected():
    """Reduced from the Wayback capture of https://firstcheck.in/fact-check-does-ivermectin-harm-reproductive-health-what-studies-actually-show/
    (the page was gone already when it was captured, and recorded as HTTP 200)"""
    markdown = ("Page Not Found - First Check\n\nThe Wayback Machine - https://web.archive.org/web/2025/x\n\n"
                "# 404\n\n### Oops... It looks like you 're lost !\n\nThe page you are looking for does not exist.")
    assert _soft_not_found(ScrapedContent(markdown=markdown)) == "Page Not Found - First Check"


def test_title_of_the_html_is_used_when_there_is_html():
    html = "<html><head><title>404 Not Found</title></head><body><h1>Nope</h1></body></html>"
    assert _soft_not_found(ScrapedContent(html=html, markdown="# Nope")) == "404 Not Found"


def test_long_article_about_a_missing_page_is_kept():
    html = ("<html><head><title>Page not found: why websites lose pages</title></head><body>"
            + "<p>" + "word " * 2000 + "</p></body></html>")
    assert _soft_not_found(ScrapedContent(html=html)) is None


def test_ordinary_page_is_kept():
    assert _soft_not_found(ScrapedContent(markdown="Does ivermectin harm health?\n\nStudies say no.")) is None
    assert _soft_not_found(ScrapedContent(html="<title>Fact check: 404 error pages</title><p>Short.</p>")) is None
