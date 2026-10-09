from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.server.paywall_detect import detect_paywall

NOT_FREE = '<script type="application/ld+json">{"@type": "NewsArticle", "isAccessibleForFree": false}</script>'
WALL = '<section class="post-detail-page__paywall"><h2>Hazte una cuenta para leer esta nota</h2></section>'


def page(body: str, declaration: str = NOT_FREE) -> ScrapedContent:
    return ScrapedContent(html=f"<html><head>{declaration}</head><body>"
                               f"<article><h1>Title</h1><p>{body}</p>{WALL}</article></body></html>")


def test_teaser_with_a_paywall_prompt_is_detected():
    """Reduced from https://www.fastcheck.cl/2023/06/01/recibimos-los-trenes-mas-rapidos-de-sudamerica-que-pronto-van-a-comenzar-a-transportar-pasajeros-entre-chillan-y-santiago-en-tan-solo-3-horas-y-40-minutos-real/
    (declares itself not free, names no paywalled part)"""
    assert detect_paywall(page("Only the first sentence of the article.")) is not None


def test_full_article_behind_a_paywall_overlay_is_kept():
    assert detect_paywall(page("word " * 300)) is None


def test_prompt_on_a_page_that_declares_itself_free_is_ignored():
    assert detect_paywall(page("Short.", declaration="")) is None


def test_declaration_without_a_prompt_is_ignored():
    content = ScrapedContent(html=f"<html><head>{NOT_FREE}</head><body><article>"
                                  f"<p>Short.</p></article></body></html>")
    assert detect_paywall(content) is None
