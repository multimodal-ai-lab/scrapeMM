"""Detection of paywalled pages whose paid part was not delivered.

News sites mark their paywalls for search engines with schema.org markup (Google's
"subscription and paywalled content" structured data): the article's JSON-LD says
`isAccessibleForFree: false`, and a `hasPart` names the paywalled element by CSS
selector. Many metered paywalls (e.g. the New York Times') still ship that element with
the full article and only hide it with JavaScript, which a scraper never runs -- so the
declaration alone says nothing. What counts is whether the declared element actually
carries the article text.

Pages that declare no selector are not judged at all: a false paywall verdict would
throw away a perfectly good article.
"""

import json
import re
from typing import Optional

from bs4 import BeautifulSoup

from scrapemm.common.scraping_response import ScrapedContent

# Fewer words than this in the paywalled part means only a teaser (or nothing) arrived
MIN_UNLOCKED_WORDS = 100
# A page that declares itself not free and shows a paywall, but names no paywalled part
# (see `_gated_reason()`), is one if its article, headline and byline included, has fewer
# words than this: Fast Check CL's teaser has 107, the shortest real articles more
MAX_GATED_WORDS = 150
GATE_REGEX = re.compile(r"paywall|gated", re.IGNORECASE)


def detect_paywall(content: ScrapedContent) -> Optional[str]:
    """Returns why the content is a paywall teaser rather than the article, or None if
    the article is there (or the page declares no paywall that could be checked)."""
    html = content.html if content else None
    if not html or "isaccessibleforfree" not in html.lower():
        return None  # Cheap check first: most pages declare no paywall at all

    soup = BeautifulSoup(html, "html.parser")
    selectors = _paywalled_selectors(soup)
    if not selectors:
        return _gated_reason(soup)

    words = 0
    for selector in selectors:
        try:
            elements = soup.select(selector)
        except Exception:
            continue  # A selector BeautifulSoup cannot parse; judge by the others
        words += sum(len(el.get_text(" ", strip=True).split()) for el in elements)

    if words >= MIN_UNLOCKED_WORDS:
        return None
    return (f"the page declares its content paywalled ({', '.join(selectors)}), and that "
            f"part carries only {words} words")


def _gated_reason(soup: BeautifulSoup) -> Optional[str]:
    """For a page that declares itself not free but names no paywalled part (Fast Check
    CL): the declaration plus a paywall element in the page (a login or subscribe prompt)
    plus little article text around it. Each alone proves nothing -- the prompt may only
    overlay a full article -- but all three do."""
    if not any(_is_false(node.get("isAccessibleForFree")) for script in
               soup.find_all("script", type="application/ld+json")
               for node in _walk(_load(script))):
        return None
    walls = soup.find_all(lambda e: GATE_REGEX.search(" ".join([str(e.get("id") or ""),
                                                                *(e.get("class") or [])])))
    if not walls:
        return None
    region = walls[0].find_parent(["article", "main"]) or soup.body or soup
    for element in [*walls, *region.find_all(["script", "style", "noscript"])]:
        if not element.decomposed:
            element.decompose()
    words = len(region.get_text(" ", strip=True).split())
    if words >= MAX_GATED_WORDS:
        return None
    return (f"the page declares itself not free and shows a paywall, with only {words} "
            f"words of article around it")


def _load(script):
    try:
        return json.loads(script.string or "")
    except ValueError:
        return None


def _paywalled_selectors(soup: BeautifulSoup) -> list[str]:
    """The CSS selectors of all parts the page's JSON-LD declares not free."""
    selectors = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        for node in _walk(data):
            if not _is_false(node.get("isAccessibleForFree")):
                continue
            parts = node.get("hasPart") or []
            for part in parts if isinstance(parts, list) else [parts]:
                if (isinstance(part, dict) and _is_false(part.get("isAccessibleForFree"))
                        and isinstance(part.get("cssSelector"), str)):
                    selectors.append(part["cssSelector"])
    return list(dict.fromkeys(selectors))


def _walk(data):
    """Every JSON object in the data, including those in lists and @graph."""
    if isinstance(data, dict):
        yield data
        for value in data.values():
            if isinstance(value, (dict, list)):
                yield from _walk(value)
    elif isinstance(data, list):
        for item in data:
            yield from _walk(item)


def _is_false(value) -> bool:
    return value is False or (isinstance(value, str) and value.strip().lower() == "false")
