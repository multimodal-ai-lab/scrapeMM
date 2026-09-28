"""Detection of CAPTCHA challenges in scraped content."""

import re
from typing import Optional

from scrapemm.common.scraping_response import ScrapedContent

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
    ("Cloudflare challenge", ("attention required! | cloudflare",
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
    # JS cookie check that re-serves the page once a cookie is set (e.g. NCBI/PMC)
    ("Cookie check", ("cookies must be enabled",)),
    # Anubis' proof-of-work check (newsmobile.in): a browser passes it by itself in a
    # second or two, while scraping services return the interstitial as the page
    ("Anubis bot check", ("anubis_challenge", "/.within.website/x/cmd/anubis",
                          "checking your browser before redirecting")),
)

# CAPTCHA widgets. They also appear on regular pages (e.g. in contact forms), hence
# they indicate a challenge only if the page carries (almost) no content.
CAPTCHA_WIDGETS: tuple[Signature, ...] = (
    ("reCAPTCHA", ("g-recaptcha", "grecaptcha", "google.com/recaptcha")),
    ("hCaptcha", ("h-captcha", "hcaptcha.com")),
    # Not "cdn-cgi/challenge-platform" as such: Cloudflare injects its passive bot
    # detection, /cdn-cgi/challenge-platform/scripts/jsd/main.js, into ordinary pages,
    # which made every short page behind Cloudflare a "Turnstile" (e.g. Perma.cc replay
    # errors). Its challenge pages load /cdn-cgi/challenge-platform/h/.../orchestrate.
    ("Cloudflare Turnstile", ("cf-turnstile", "challenges.cloudflare.com",
                              "cdn-cgi/challenge-platform/h/")),
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
# A JSON key named "captcha" is configuration, not a challenge: every TikTok page ships
# {"captcha": "//verification-..."} in its api-domains script, which flagged TikTok's
# not-yet-rendered page shell as a CAPTCHA
JSON_CAPTCHA_KEY_REGEX = re.compile(r"""["']captcha["']\s*:""")


def detect_captcha(content: ScrapedContent) -> Optional[str]:
    """Checks whether the scraped content is a CAPTCHA challenge instead of the actual
    page content. Returns the name of the detected CAPTCHA, None if there is none."""
    if not content:
        return None

    markup, text = _extract_texts(content)

    # Markers that are unambiguous on their own
    if label := _find(markup, CONCLUSIVE_MARKERS):
        return label

    # Everything else counts only if there is no real content to speak of. A video is
    # real content however little text comes with it: a TikTok capture has barely any,
    # while its markup mentions "captcha". Matches a <video> tag in the HTML as well as
    # a video item in multimodal output, so it holds for every output format.
    if "<video" in markup:
        return None
    if len(text) <= MAX_CHALLENGE_TEXT_LENGTH:
        return _find(markup, CHALLENGE_PHRASES) or _find(markup, CAPTCHA_WIDGETS)

    return None


def explain(content: ScrapedContent) -> dict:
    """Says why `detect_captcha()` decides as it does: every marker present, and whether
    the page is short enough for the non-conclusive ones to count. For debugging
    detections a human reported as false."""
    markup, text = _extract_texts(content)
    groups = (("conclusive", CONCLUSIVE_MARKERS), ("phrase", CHALLENGE_PHRASES),
              ("widget", CAPTCHA_WIDGETS))
    return {
        "verdict": detect_captcha(content),
        "text_length": len(text),
        "short_page": len(text) <= MAX_CHALLENGE_TEXT_LENGTH,
        "matches": [f"{kind}: {label} ({marker!r})"
                    for kind, signatures in groups
                    for label, markers in signatures
                    for marker in markers if marker in markup],
    }


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

    markup = JSON_CAPTCHA_KEY_REGEX.sub("", "\n".join(markup_parts).lower())
    return markup, " ".join((text or "").split())
