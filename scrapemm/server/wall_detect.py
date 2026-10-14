"""Detection of pages that stand in for the content: a login page, an age check, a notice
that the page needs JavaScript (or the app's error screen), and a document's raw bytes
passed off as its text. Each scraped "successfully", so without these checks the wall
was returned -- and cached -- as the content.

Every check asks for a specific marker *and* next to no text besides: a real article
that mentions logins, age checks or JavaScript is long, and must never be thrown away."""

import re
from typing import Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from scrapemm.common.scraping_response import ScrapedContent

# Words a wall shows at most besides its marker: a login form with its links and footer,
# an age check with its legal notice
WALL_MAX_WORDS = 300

# Paths of login and age-check pages: when one was asked for, it is the content
LOGIN_PATH = re.compile(r"/(?:login|log-in|signin|sign-in|sign_in|uas/login|authwall|accounts/login|"
                        r"auth/login|age-verify|age-verification|age-gate|agegate)(?:[/?#.]|$)",
                        re.IGNORECASE)
_LOGIN_HEADING = re.compile(
    r"\b(log\s*in|login|sign\s*in|sign\s*up|einloggen|anmelden|anmeldung|iniciar\s+sesi[oó]n|"
    r"connexion|se\s+connecter|accedi|entrar|zaloguj|вход|войти|авторизац\w*)\b", re.IGNORECASE)

AGE_GATE_PHRASES = (
    "are you 18 years of age or older", "are you over 18", "are you 18 or older",
    "you must be 18 years or older", "you must be at least 18", "verify your age",
    "confirm your age", "age verification", "bist du mindestens 18", "sind sie mindestens 18",
    "altersverifikation", "altersbestätigung", "¿eres mayor de edad", "êtes-vous majeur",
)

# A page that shows only that it needs JavaScript, or an app's error screen: seen in
# archive captures of script-rendered pages (kremlin.ru, X)
NO_CONTENT_PHRASES = (
    "enable javascript", "javascript is disabled", "javascript is not available",
    "javascript is required", "requires javascript", "turn on javascript",
    "включите поддержку javascript", "включите javascript", "javascript aktivieren",
    "activez javascript", "activer javascript", "habilita javascript", "activa javascript",
    "something went wrong, but don't fret", "something went wrong. try reloading",
    # Instagram's player in an archive's capture of a reel
    "sorry, we're having trouble playing this video",
)
NO_CONTENT_MAX_WORDS = 150  # X's page without JavaScript, its footer links included: 88


def _visible_text(soup) -> str:
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    return " ".join(soup.get_text(" ").split())


def _text(content: ScrapedContent) -> str:
    return content.markdown or (str(content.multimodal) if content.multimodal is not None else "")


def login_wall(url: str, content: ScrapedContent) -> Optional[str]:
    """The heading of a login page shown instead of the content: a password field, a
    login heading, and next to no text. Not if a login page was asked for."""
    html = content.html or ""
    if 'type="password"' not in html.lower().replace("'", '"') or LOGIN_PATH.search(urlsplit(url).path):
        return None
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select("input[type=password]"):
        return None
    headings = [soup.title.get_text(" ", strip=True) if soup.title else ""] + \
               [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])[:3]]
    if len(_visible_text(soup).split()) > WALL_MAX_WORDS:
        return None
    return next((h[:120] for h in headings if h and _LOGIN_HEADING.search(h)), None)


def age_gate(content: ScrapedContent) -> Optional[str]:
    """The phrase of an age check shown instead of the content."""
    if content.html:
        soup = BeautifulSoup(content.html, "html.parser")
        text = _visible_text(soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup)
    else:
        text = _text(content)
    if len(text.split()) > 2 * WALL_MAX_WORDS:
        return None
    lowered = text.lower().replace("’", "'")
    return next((phrase for phrase in AGE_GATE_PHRASES if phrase in lowered), None)


def no_content_notice(content: ScrapedContent) -> Optional[str]:
    """The phrase of a page that only says it needs JavaScript, or of an app's error
    screen, if that is all there is."""
    text = _text(content) or (_visible_text(BeautifulSoup(content.html, "html.parser")) if content.html else "")
    if not text or len(text.split()) > NO_CONTENT_MAX_WORDS:
        return None
    lowered = text.lower().replace("’", "'")
    return next((phrase for phrase in NO_CONTENT_PHRASES if phrase in lowered), None)


def raw_document(content: ScrapedContent) -> bool:
    """Whether the "text" is a PDF's raw bytes, as a method that does not read documents
    passes them on."""
    text = (content.markdown or content.html or "").lstrip("`\r\n \t")
    return text.startswith("%PDF-") or ("%PDF-1." in text[:1024] and "endobj" in text[:65536])
