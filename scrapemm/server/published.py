"""When a page says it was published, from its metadata: for content whose source states no
time of its own (see `ScrapedContent.published_at`).

Pages declare it for search engines and reference managers -- `article:published_time`
(Open Graph), `datePublished` (schema.org JSON-LD), `citation_publication_date` (Google
Scholar, e.g. PubMed Central), Dublin Core. Scholarly ones often know only the year or the
month ("2013", "2020 Feb 3"), which is kept at that precision rather than made up."""

import re
from datetime import datetime
from typing import Optional

from scrapemm.common.scraping_response import utc_iso

# In order of preference: an article's own declaration first, the looser ones last
_META_NAMES = ("article:published_time", "og:published_time", "citation_publication_date",
               "citation_date", "citation_online_date", "dc.date.issued", "dcterms.issued",
               "dc.date", "dcterms.date", "date", "pubdate", "publish-date", "sailthru.date")
_META = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""\b(name|property|itemprop|content)\s*=\s*(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)
_JSON_LD_DATE = re.compile(r'"datePublished"\s*:\s*"([^"]{4,40})"')
# How much of the page is searched: the metadata stand in the head
HEAD_CHARS = 300_000

# Dates that name less than a moment, with the precision they are kept at
_PARTIAL_FORMATS = (("%Y-%m-%d", "%Y-%m-%d"), ("%Y/%m/%d", "%Y-%m-%d"), ("%Y %b %d", "%Y-%m-%d"),
                    ("%d %B %Y", "%Y-%m-%d"), ("%B %d, %Y", "%Y-%m-%d"), ("%Y%m%d", "%Y-%m-%d"),
                    ("%Y-%m", "%Y-%m"), ("%Y/%m", "%Y-%m"), ("%Y %b", "%Y-%m"), ("%Y", "%Y"))


def published_from_html(html: Optional[str]) -> Optional[str]:
    """When the page says it was published (see `ScrapedContent.published_at`), or None."""
    if not html:
        return None
    head = html[:HEAD_CHARS]
    declared: dict[str, str] = {}
    for tag in _META.findall(head):
        attributes = {k.lower(): v for k, _, v in _ATTR.findall(tag)}
        key = (attributes.get("name") or attributes.get("property") or attributes.get("itemprop") or "").lower()
        if key in _META_NAMES and attributes.get("content") and key not in declared:
            declared[key] = attributes["content"].strip()
    if match := _JSON_LD_DATE.search(head):
        declared.setdefault("datepublished", match.group(1))
    for key in ("article:published_time", "og:published_time", "datepublished", *_META_NAMES[2:]):
        if key in declared and (when := normalize(declared[key])):
            return when
    return None


def normalize(raw: str) -> Optional[str]:
    """A declared date in ISO 8601: a moment in UTC, or a day, month or year as given."""
    raw = raw.strip()
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        when = None
    if when is not None and ("T" in raw or " " in raw.strip()):
        return utc_iso(when)  # A moment (one without a zone taken as UTC)
    for pattern, precision in _PARTIAL_FORMATS:
        try:
            return datetime.strptime(raw, pattern).strftime(precision)
        except ValueError:
            continue
    # E.g. "2013 Spring", "2013 Jan-Feb": the year at least
    if (match := re.match(r"(1[5-9]|20)\d{2}\b", raw)) and len(raw) <= 20:
        return match.group(0)
    return None
