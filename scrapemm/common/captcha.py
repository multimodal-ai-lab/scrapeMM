"""Detection of CAPTCHA challenges in scraped content."""

import re
from typing import Optional

from .scraping_response import ScrapedContent

# A signature is a label along with the (lowercase) markers indicating it
Signature = tuple[str, tuple[str, ...]]

# Markers that occur on CAPTCHA challenge pages only, no matter the page's length
CONCLUSIVE_MARKERS: tuple[Signature, ...] = (
    ("DataDome CAPTCHA", ("captcha-delivery.com",)),
    ("AWS WAF CAPTCHA", ("captcha.awswaf.com", "awswaf.com/captcha")),
    ("PerimeterX CAPTCHA", ("px-captcha", "captcha.px-cdn.net")),
    ("Imperva/Incapsula bot check", ("incapsula incident id",)),
)

# Phrases typical for CAPTCHA and bot-check interstitials
CHALLENGE_PHRASES: tuple[Signature, ...] = (
    ("Cloudflare challenge", ("just a moment...", "attention required! | cloudflare",
                              "checking if the site connection is secure",
                              "enable javascript and cookies to continue",
                              "verify you are human", "verifying you are human")),
    ("CAPTCHA challenge", ("i'm not a robot", "i am not a robot", "are you a robot",
                           "are you human", "complete the security check",
                           "performing security verification", "complete the captcha",
                           "solve the captcha", "enter the characters you see",
                           "type the characters you see in this image",
                           "press and hold to confirm",
                           "unusual traffic from your computer network")),
)

# CAPTCHA widgets. They also appear on regular pages (e.g. in contact forms), hence
# they indicate a challenge only if the page carries (almost) no content.
CAPTCHA_WIDGETS: tuple[Signature, ...] = (
    ("reCAPTCHA", ("g-recaptcha", "grecaptcha", "google.com/recaptcha")),
    ("hCaptcha", ("h-captcha", "hcaptcha.com")),
    ("Cloudflare Turnstile", ("cf-turnstile", "challenges.cloudflare.com",
                              "cdn-cgi/challenge-platform")),
    ("Arkose FunCaptcha", ("funcaptcha", "arkoselabs")),
    ("GeeTest CAPTCHA", ("geetest",)),
    # Markup of unknown CAPTCHA implementations. The bare word "captcha" is deliberately
    # not a marker: it also occurs in ordinary page text (a false positive once got
    # perma.cc blacklisted).
    ("CAPTCHA", ('"captcha"', "'captcha'", "captcha-container", "captcha-form",
                 "captcha_form", "/captcha/", "captcha challenge", "captcha required")),
)

# Challenge pages show hardly any text. Anything longer is considered actual content.
MAX_CHALLENGE_TEXT_LENGTH = 1500

SCRIPT_REGEX = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
TAG_REGEX = re.compile(r"<[^>]+>")


def detect_captcha(content: ScrapedContent) -> Optional[str]:
    """Checks whether the scraped content is a CAPTCHA challenge instead of the actual
    page content. Returns the name of the detected CAPTCHA, None if there is none."""
    if not content:
        return None

    markup, text = _extract_texts(content)

    # Markers that are unambiguous on their own
    if label := _find(markup, CONCLUSIVE_MARKERS):
        return label

    # Everything else counts only if there is no real content to speak of
    if len(text) <= MAX_CHALLENGE_TEXT_LENGTH:
        return _find(markup, CHALLENGE_PHRASES) or _find(markup, CAPTCHA_WIDGETS)

    return None


def _find(markup: str, signatures: tuple[Signature, ...]) -> Optional[str]:
    """Returns the label of the first signature present in the given markup."""
    for label, markers in signatures:
        if any(marker in markup for marker in markers):
            return label
    return None


def _extract_texts(content: ScrapedContent) -> tuple[str, str]:
    """Returns the lowercased content twice: once with all of its markup (where the
    CAPTCHA markers live) and once as the visible text only (to measure its length)."""
    markup_parts = []
    text = None
    if content.markdown is not None:
        markup_parts.append(content.markdown)
        text = content.markdown
    if content.multimodal is not None:
        rendered = str(content.multimodal)
        markup_parts.append(rendered)
        text = text or rendered
    if content.html is not None:
        markup_parts.append(content.html)
        if text is None:
            text = TAG_REGEX.sub(" ", SCRIPT_REGEX.sub(" ", content.html))

    return "\n".join(markup_parts).lower(), " ".join((text or "").split())
