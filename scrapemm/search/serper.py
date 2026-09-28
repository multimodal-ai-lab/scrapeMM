"""Serper (serper.dev): Google search results as JSON.

The classes mirror Serper's API as it is documented and as it answers: the query holds
Serper's own parameters under their own names, and the response has one typed
attribute per field Serper returns, spelled in snake case (`imageUrl` becomes
`image_url`). Whatever Serper adds beyond these lands in `extra`, on every level.

    from scrapemm.search import SerperQuery, search

    response = await search(SerperQuery(q="eiffel tower", type="images", num=20))
    for image in response.images:
        print(image.image_url, image.link)
"""

from __future__ import annotations

# As a module: several result classes have a field called `date`, which a bare `date`
# type in their annotations would be evaluated against
import datetime as dt
import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from .base import SearchQuery, SearchResponse, WireRecord

SerperType = Literal["search", "images"]
SERPER_TYPES = ("search", "images")

# Google reads no more than this many words of a query; every -site: is one of them
GOOGLE_MAX_WORDS = 32


@dataclass
class _SerperRecord(WireRecord):
    WIRE_CASE = "camel"


@dataclass
class SerperSearchParameters(_SerperRecord):
    """Serper's echo of the query it answered."""

    q: Optional[str] = None
    type: Optional[str] = None
    engine: Optional[str] = None
    gl: Optional[str] = None
    hl: Optional[str] = None
    num: Optional[int] = None
    page: Optional[int] = None
    location: Optional[str] = None
    tbs: Optional[str] = None
    autocorrect: Optional[bool] = None


@dataclass
class SerperKnowledgeGraph(_SerperRecord):
    title: Optional[str] = None
    type: Optional[str] = None
    website: Optional[str] = None
    image_url: Optional[str] = None
    description: Optional[str] = None
    description_source: Optional[str] = None
    description_link: Optional[str] = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class SerperAnswerBox(_SerperRecord):
    title: Optional[str] = None
    answer: Optional[str] = None
    snippet: Optional[str] = None
    snippet_highlighted: list[str] = field(default_factory=list)
    link: Optional[str] = None
    date: Optional[str] = None


@dataclass
class SerperSitelink(_SerperRecord):
    title: Optional[str] = None
    link: Optional[str] = None


@dataclass
class SerperOrganicResult(_SerperRecord):
    title: Optional[str] = None
    link: Optional[str] = None
    snippet: Optional[str] = None
    date: Optional[str] = None
    position: Optional[int] = None
    sitelinks: list[SerperSitelink] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    rating: Optional[float] = None
    rating_count: Optional[int] = None


@dataclass
class SerperPeopleAlsoAsk(_SerperRecord):
    question: Optional[str] = None
    snippet: Optional[str] = None
    title: Optional[str] = None
    link: Optional[str] = None


@dataclass
class SerperRelatedSearch(_SerperRecord):
    query: Optional[str] = None


@dataclass
class SerperTopStory(_SerperRecord):
    title: Optional[str] = None
    link: Optional[str] = None
    source: Optional[str] = None
    date: Optional[str] = None
    image_url: Optional[str] = None


@dataclass
class SerperImage(_SerperRecord):
    """One result of an image search. `link` is the page showing the image,
    `image_url` the image itself."""

    title: Optional[str] = None
    image_url: Optional[str] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    thumbnail_url: Optional[str] = None
    thumbnail_width: Optional[int] = None
    thumbnail_height: Optional[int] = None
    source: Optional[str] = None
    domain: Optional[str] = None
    link: Optional[str] = None
    google_url: Optional[str] = None
    position: Optional[int] = None


@dataclass
class SerperResponse(SearchResponse, _SerperRecord):
    """Serper's answer. A web search fills `organic` and whichever of the other blocks
    Google showed; an image search fills `images`."""

    provider = "serper"

    search_parameters: Optional[SerperSearchParameters] = None
    knowledge_graph: Optional[SerperKnowledgeGraph] = None
    answer_box: Optional[SerperAnswerBox] = None
    organic: list[SerperOrganicResult] = field(default_factory=list)
    people_also_ask: list[SerperPeopleAlsoAsk] = field(default_factory=list)
    related_searches: list[SerperRelatedSearch] = field(default_factory=list)
    top_stories: list[SerperTopStory] = field(default_factory=list)
    images: list[SerperImage] = field(default_factory=list)
    credits: Optional[int] = None

    @property
    def urls(self) -> list[str]:
        """The result pages: the organic results of a web search, and for an image
        search the pages the images appear on (see `image_urls` for the images)."""
        kind = self.search_parameters.type if self.search_parameters else None
        if kind == "images" or (kind is None and not self.organic):
            links = [image.link for image in self.images]
        else:
            links = [result.link for result in self.organic]
        return list(dict.fromkeys(link for link in links if link))

    @property
    def image_urls(self) -> list[str]:
        return list(dict.fromkeys(image.image_url for image in self.images
                                  if image.image_url))

    def without_sites(self, sites: list[str]) -> "SerperResponse":
        """This response without anything that points to one of `sites` (domains,
        subdomains included): results, images, top stories, questions and the answer
        box. Positions stay Google's, so they may have gaps."""
        if not sites:
            return self

        def keep(items: list) -> list:
            return [item for item in items if not _on_sites(item.link, sites)]

        return replace(
            self,
            organic=keep(self.organic),
            images=keep(self.images),
            top_stories=keep(self.top_stories),
            people_also_ask=keep(self.people_also_ask),
            answer_box=(None if self.answer_box and _on_sites(self.answer_box.link, sites)
                        else self.answer_box),
        )


@dataclass
class SerperQuery(SearchQuery[SerperResponse]):
    """A query to Serper. `type` picks the endpoint; everything else is Serper's own
    parameter of the same name, and None leaves it to Serper's default.

    :param q: The search terms.
    :param type: "search" (web results) or "images".
    :param gl: Country to search from, as a two-letter code ("us", "de").
    :param hl: Language of the results, as a language code ("en", "de").
    :param location: Place to search from, e.g. "Berlin, Germany".
    :param num: Number of results, 1 to 100 (Serper's default: 10).
    :param page: Page of results, starting at 1.
    :param tbs: Time filter in Google's notation: "qdr:h", "qdr:d", "qdr:w", "qdr:m"
        or "qdr:y" for the past hour, day, week, month or year.
    :param autocorrect: Whether Google may correct the spelling of `q` (default: yes).
    :param before: Only results from before this day: the day itself and everything
        after it are left out. A `datetime.date` or an ISO string ("2024-05-01"). Not a
        parameter of Serper's own but scrapeMM's, sent as Google's custom date range in
        `tbs`, so it cannot be combined with a "qdr:" time filter there. Google dates a
        page by when it was published or first indexed, which is not always exact.
    :param exclude_sites: Websites to leave out, as domains ("snopes.com"; a URL is
        reduced to its domain). Subdomains go with them. Also scrapeMM's own: sent to
        Google as -site: operators, as many as fit its 32-word limit, and whatever gets
        through anyway is removed from the response -- so a page may hold fewer than
        `num` results.
    """

    # Plain class attributes, not annotated again: the base class declares them, and
    # an annotation mentioning `type` would be evaluated against the field of that name
    provider = "serper"
    response_class = SerperResponse

    q: str
    type: SerperType = "search"
    gl: Optional[str] = None
    hl: Optional[str] = None
    location: Optional[str] = None
    num: Optional[int] = None
    page: Optional[int] = None
    tbs: Optional[str] = None
    autocorrect: Optional[bool] = None
    before: Optional[dt.date] = None
    exclude_sites: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not isinstance(self.q, str) or not self.q.strip():
            raise ValueError("The search terms (q) must not be empty.")
        if self.type not in SERPER_TYPES:
            raise ValueError(f"Serper has no search type '{self.type}'. "
                             f"Available: {', '.join(SERPER_TYPES)}.")
        if self.num is not None and not 1 <= self.num <= 100:
            raise ValueError(f"num must be between 1 and 100, not {self.num}.")
        if self.page is not None and self.page < 1:
            raise ValueError(f"page must be 1 or more, not {self.page}.")
        if self._before() is not None and self.tbs and ("qdr:" in self.tbs
                                                        or "cdr:" in self.tbs):
            raise ValueError(f"before cannot be combined with the time filter "
                             f"tbs={self.tbs!r}: both restrict the date. Use one of them.")
        if not isinstance(self.exclude_sites, list):
            raise ValueError("exclude_sites must be a list of domains.")
        for site in self.exclude_sites:
            if not isinstance(site, str) or not _DOMAIN.fullmatch(_domain_of(site)):
                raise ValueError(f"exclude_sites must hold domains like 'example.com', "
                                 f"not {site!r}.")

    @property
    def excluded_domains(self) -> list[str]:
        """`exclude_sites` as bare domains, without duplicates."""
        return list(dict.fromkeys(_domain_of(site) for site in self.exclude_sites))

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        try:
            before = self._before()
        except ValueError:
            return data  # Travels as given, for validation to refuse
        if before is not None:
            data["before"] = before.isoformat()  # The day, also if given as a datetime
        return data

    def request_body(self) -> dict[str, Any]:
        # The type is not a parameter to Serper: it is the endpoint the body goes to
        body = self.to_dict()
        body.pop("type", None)
        body.pop("before", None)
        body.pop("exclude_sites", None)
        if domains := self.excluded_domains:
            # Google stops reading after its word limit, so only as many operators go
            # along as fit behind the search terms; the rest is filtered afterwards
            room = max(GOOGLE_MAX_WORDS - len(self.q.split()), 0)
            operators = " ".join(f"-site:{domain}" for domain in domains[:room])
            if operators:
                body["q"] = f"{self.q} {operators}"
        if (before := self._before()) is not None:
            # Google's range is inclusive and M/D/Y; the open start is spelled as a date
            # long before the web, which Google accepts more reliably than none at all
            last = before - dt.timedelta(days=1)
            window = f"cdr:1,cd_min:1/1/1900,cd_max:{last.month}/{last.day}/{last.year}"
            body["tbs"] = f"{self.tbs},{window}" if self.tbs else window
        return body

    def _before(self) -> Optional[dt.date]:
        """`before` as a date; it may have been given as an ISO string."""
        if self.before is None or type(self.before) is dt.date:
            return self.before
        if isinstance(self.before, dt.datetime):
            return self.before.date()
        if isinstance(self.before, str):
            try:
                return dt.date.fromisoformat(self.before.strip())
            except ValueError:
                pass
        raise ValueError(f"before must be a date like '2024-05-01', not {self.before!r}.")


# A hostname: labels of letters, digits and hyphens, at least one dot
_DOMAIN = re.compile(r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}")


def _domain_of(site: str) -> str:
    """The bare domain of a site or URL: https://www.Snopes.com/x becomes snopes.com."""
    site = site.strip().lower()
    host = urlsplit(site if "//" in site else f"//{site}").hostname or ""
    return host.removeprefix("www.")


def _on_sites(url: Optional[str], domains: list[str]) -> bool:
    """Whether `url` belongs to one of `domains`, subdomains included."""
    if not url:
        return False
    host = _domain_of(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)
