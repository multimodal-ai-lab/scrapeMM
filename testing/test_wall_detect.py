"""Pages that stand in for the content -- login pages, age checks, consent-or-pay walls,
JavaScript notices, raw PDF bytes, error pages -- are no success. Built from the pages
the sites actually served (October 2026)."""

import pytest

from scrapemm.common import AccessBlockedError, RetrievalFailed
from scrapemm.common.exceptions import PaywallError, TargetUnavailableError
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.server.engine import _access_denied_page, _classify, _soft_not_found
from scrapemm.server.paywall_detect import detect_paywall
from scrapemm.server.wall_detect import login_wall

pytestmark = pytest.mark.server

DOJ = ('<title>Age Verification | United States Department of Justice</title><main><div id="age-verify-block">'
       '<h2>Are you 18 years of age or older?</h2><button id="age-button-yes">Yes</button><button>No</button>'
       '<h4>Access Denied</h4><p>You must be 18 years or older to access this content.</p></div></main>')
LINKEDIN = ('<title>LinkedIn Login, Einloggen | LinkedIn</title><h1>Einloggen</h1><p>Neu bei LinkedIn? '
            'Jetzt anmelden</p><form><input type="text" name="session_key">'
            '<input type="password" name="session_password"></form>')
RP_ONLINE = ('<meta name="robots" content="noindex"><title>Aktuelle Nachrichten | Rheinische Post Online</title>'
             '<h1>Wie möchten Sie die Rheinische Post lesen? Sie haben die Wahl!</h1><p>Weiter mit Werbung</p>'
             '<p>Ich möchte die Inhalte der Rheinischen Post inkl. aller RP+ Artikel komplett werbefrei lesen</p>')
ARTICLE = "<article>" + "<p>" + "Ein ganz gewöhnlicher Satz eines Artikels. " * 60 + "</p></article>"


def classify(url, html=None, markdown=None, method="Browser"):
    return _classify(url, method, ScrapedContent(html=html, markdown=markdown),
                     "markdown" if markdown is not None else "html")


def test_an_age_check_is_no_content():
    status, error = classify("https://www.justice.gov/epstein/files/DataSet 9/EFTA00654215.pdf", DOJ)
    assert status == "error" and isinstance(error, AccessBlockedError) and not isinstance(error, PaywallError)


def test_a_login_page_is_no_content_unless_asked_for():
    status, error = classify("https://www.linkedin.com/company/x/posts/", LINKEDIN)
    assert status == "error" and isinstance(error, AccessBlockedError)
    assert login_wall("https://github.com/login", ScrapedContent(html=LINKEDIN)) is None
    long_page = LINKEDIN.replace("</form>", "</form><p>" + "word " * 400 + "</p>")
    assert login_wall("https://x.test/a", ScrapedContent(html=long_page)) is None


def test_a_consent_or_pay_wall_is_a_paywall():
    assert detect_paywall(ScrapedContent(html=RP_ONLINE))
    assert detect_paywall(ScrapedContent(html=RP_ONLINE + ARTICLE)) is None  # The article stands below


def test_a_declared_paywall_that_names_nothing_judges_nothing():
    # CNN's live blogs declare `.layout-live-story__wrapper` paywalled, which they lack
    ld = ('<script type="application/ld+json">{"@type": "NewsArticle", "isAccessibleForFree": false, '
          '"hasPart": {"@type": "WebPageElement", "isAccessibleForFree": false, '
          '"cssSelector": ".layout-live-story__wrapper"}}</script>')
    assert detect_paywall(ScrapedContent(html=ld + ARTICLE)) is None
    teaser = ld.replace(".layout-live-story__wrapper", ".paid") + '<div class="paid"></div><p>Teaser.</p>'
    assert detect_paywall(ScrapedContent(html=teaser))


def test_a_javascript_notice_is_no_content():
    markdown = ("Владимир Путин – личный сайт\nThe Wayback Machine - https://web.archive.org/web/"
                "20260128141421/http://en.putin.kremlin.ru/bio/page-0\n"
                "Для работы приложения включите поддержку JavaScript и обновите страницу.")
    status, error = classify("https://web.archive.org/web/20260128141421/http://en.putin.kremlin.ru/bio/page-0",
                             markdown=markdown, method="Internet Archive")
    assert status == "error" and isinstance(error, RetrievalFailed)
    article = "Please enable JavaScript to comment. " + "This is the article itself. " * 40
    assert classify("https://news.test/a", markdown=article)[0] == "success"


def test_raw_pdf_bytes_are_no_text():
    raw = "%PDF-1.2\n%�\n52 0 obj\n<<\n/Linearized 1\n>>\nendobj\n"
    status, error = classify("https://www.oas.org/x.pdf", markdown=raw, method="Decodo")
    assert status == "error" and isinstance(error, RetrievalFailed)


def test_error_pages():
    html = ("<title>Fehler 404 – Seite nicht gefunden</title><h1>Fehler 404 – Seite nicht gefunden</h1>"
            "<p>Leider ist die gewünschte Seite unter dieser Adresse nicht auffindbar.</p>")
    assert _soft_not_found(ScrapedContent(html=html)) == "Fehler 404 – Seite nicht gefunden"
    status, error = classify("https://www.handelsblatt.com/x.html", html)
    assert isinstance(error, TargetUnavailableError)
    waf = ("Not Acceptable\n\n# 406 - Not Acceptable\n\nRequest was blocked due to suspicious behavior.\n\n"
           "## Request Details\n| **ID** | 2cb6caba |")
    assert _access_denied_page(ScrapedContent(markdown=waf))


def test_ordinary_articles_pass():
    page = "<title>Login-Probleme bei Banken</title><h1>Wie man sich anmelden kann</h1>" + ARTICLE
    assert classify("https://news.test/login-probleme", page)[0] == "success"
