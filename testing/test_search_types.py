"""The search types that ship with the client: the plumbing every provider's classes
share, and Serper's classes, which must mirror Serper's JSON without losing any of it."""

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

from scrapemm.search import (SearchResponse, SerperImage, SerperOrganicResult, SerperQuery,
                             SerperResponse, SerperSitelink, WireRecord)

DATA = Path(__file__).parent / "data"


def sample(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


# --- The shared plumbing ----------------------------------------------------------

@dataclass
class Leaf(WireRecord):
    WIRE_CASE = "camel"
    image_url: Optional[str] = None


@dataclass
class Tree(WireRecord):
    WIRE_CASE = "camel"
    top_leaf: Optional[Leaf] = None
    many_leaves: list[Leaf] = field(default_factory=list)
    renamed: Optional[int] = field(default=None, metadata={"wire": "Odd-Key"})


def test_keys_are_translated_both_ways():
    data = {"topLeaf": {"imageUrl": "a"}, "manyLeaves": [{"imageUrl": "b"}], "Odd-Key": 3}
    tree = Tree.from_dict(data)
    assert tree.top_leaf == Leaf(image_url="a")
    assert tree.many_leaves == [Leaf(image_url="b")]
    assert tree.renamed == 3
    assert tree.to_dict() == data


def test_unknown_keys_are_kept_on_every_level():
    data = {"topLeaf": {"imageUrl": "a", "newField": [1]}, "brandNew": {"x": 1}}
    tree = Tree.from_dict(data)
    assert tree.extra == {"brandNew": {"x": 1}}
    assert tree.top_leaf.extra == {"newField": [1]}
    assert tree.to_dict() == data


def test_nothing_is_left_in_that_carries_no_information():
    assert Tree().to_dict() == {}
    assert Tree(top_leaf=Leaf(), many_leaves=[]).to_dict() == {}
    assert Tree(renamed=0).to_dict() == {"Odd-Key": 0}  # Falsy, but information


def test_values_are_taken_as_they_come():
    """A provider sending an unexpected type must not make parsing fail."""
    assert Leaf.from_dict({"imageUrl": 5}).image_url == 5
    assert Tree.from_dict({"manyLeaves": "not a list"}).many_leaves == "not a list"


def test_only_objects_are_accepted():
    with pytest.raises(ValueError):
        Tree.from_dict([1, 2])


def test_a_query_without_its_required_field_is_refused():
    with pytest.raises(ValueError, match="q"):
        SerperQuery.from_dict({"num": 5})


def test_a_query_travels_under_its_field_names():
    """The server validates a query against the dataclass itself, so its keys must be
    the field names; unknown ones are dropped, as the server would drop them."""
    query = SerperQuery.from_dict({"q": "x", "num": 5, "bogus": 1})
    assert query == SerperQuery(q="x", num=5)
    assert query.to_dict() == {"q": "x", "type": "search", "num": 5}


def test_the_base_response_has_no_urls_of_its_own():
    with pytest.raises(NotImplementedError):
        SearchResponse().urls


# --- Serper -----------------------------------------------------------------------

@pytest.mark.parametrize("name", ["serper_search.json", "serper_images.json"])
def test_serper_answers_survive_a_round_trip(name):
    data = sample(name)
    assert SerperResponse.from_dict(data).to_dict() == data


def test_a_web_search_is_typed_through():
    response = SerperResponse.from_dict(sample("serper_search.json"))
    assert response.search_parameters.q == "apple inc"
    assert response.search_parameters.type == "search"
    assert response.knowledge_graph.image_url.startswith("https://")
    assert response.knowledge_graph.description_source == "Wikipedia"
    assert response.knowledge_graph.attributes["CEO"].startswith("Tim Cook")
    assert response.answer_box.snippet_highlighted == ["American multinational technology company"]

    first = response.organic[0]
    assert isinstance(first, SerperOrganicResult)
    assert first.link == "https://www.apple.com/"
    assert first.position == 1
    assert first.sitelinks[0] == SerperSitelink(title="Support", link="https://support.apple.com/")
    assert response.organic[1].attributes["Products"].startswith("iPhone")
    assert response.organic[2].rating_count == 1203
    assert response.organic[2].date == "2 days ago"

    assert response.top_stories[0].source == "Reuters"
    assert response.people_also_ask[0].question == "What does Apple Inc. do?"
    assert [r.query for r in response.related_searches] == ["apple inc stock",
                                                           "apple inc headquarters"]
    assert response.credits == 1
    assert response.images == []


def test_an_image_search_is_typed_through():
    response = SerperResponse.from_dict(sample("serper_images.json"))
    image = response.images[0]
    assert isinstance(image, SerperImage)
    assert image.image_width == 1200 and image.image_height == 2400
    assert image.thumbnail_url.startswith("https://encrypted-tbn0.gstatic.com/")
    assert image.link == "https://en.wikipedia.org/wiki/Eiffel_Tower"
    assert image.source == "Wikipedia"
    assert image.domain == "en.wikipedia.org"
    assert response.organic == []


def test_urls_are_the_result_pages():
    web = SerperResponse.from_dict(sample("serper_search.json"))
    assert web.urls == ["https://www.apple.com/", "https://en.wikipedia.org/wiki/Apple_Inc.",
                        "https://finance.yahoo.com/quote/AAPL/"]

    images = SerperResponse.from_dict(sample("serper_images.json"))
    assert images.urls[0] == "https://en.wikipedia.org/wiki/Eiffel_Tower"
    assert images.image_urls[0].startswith("https://upload.wikimedia.org/")
    assert len(images.urls) == len(images.image_urls) == 3


def test_urls_skip_duplicates_and_gaps():
    response = SerperResponse(organic=[SerperOrganicResult(link="https://a"),
                                       SerperOrganicResult(link=None),
                                       SerperOrganicResult(link="https://a")])
    assert response.urls == ["https://a"]


def test_a_query_knows_its_provider_and_answer():
    assert SerperQuery.provider == SerperResponse.provider == "serper"
    assert SerperQuery.response_class is SerperResponse


@pytest.mark.parametrize("query, problem", [
    (SerperQuery(q=""), "must not be empty"),
    (SerperQuery(q="   "), "must not be empty"),
    (SerperQuery(q="x", type="videos"), "no search type 'videos'"),
    (SerperQuery(q="x", num=0), "between 1 and 100"),
    (SerperQuery(q="x", num=101), "between 1 and 100"),
    (SerperQuery(q="x", page=0), "1 or more"),
])
def test_invalid_queries_are_refused(query, problem):
    with pytest.raises(ValueError, match=problem):
        query.validate()


def test_the_request_to_serper_holds_only_serper_parameters():
    query = SerperQuery(q="eiffel tower", type="images", gl="fr", num=20, tbs="qdr:w")
    query.validate()
    assert query.request_body() == {"q": "eiffel tower", "gl": "fr", "num": 20,
                                    "tbs": "qdr:w"}


# --- Serper: before a date ----------------------------------------------------------

@pytest.mark.parametrize("before", [dt.date(2024, 5, 1), "2024-05-01",
                                    dt.datetime(2024, 5, 1, 18, 30)])
def test_before_excludes_the_day_itself(before):
    """Google's custom range is inclusive, so "before May 1" must end on April 30."""
    query = SerperQuery(q="x", before=before)
    query.validate()
    assert query.request_body() == {"q": "x",
                                    "tbs": "cdr:1,cd_min:1/1/1900,cd_max:4/30/2024"}


def test_before_crosses_month_and_year_boundaries():
    assert SerperQuery(q="x", before="2025-01-01").request_body()["tbs"].endswith(
        "cd_max:12/31/2024")
    assert SerperQuery(q="x", before="2024-03-01").request_body()["tbs"].endswith(
        "cd_max:2/29/2024")  # A leap year


def test_before_travels_as_the_day_and_comes_back_as_a_date():
    query = SerperQuery(q="x", before=dt.datetime(2024, 5, 1, 18, 30))
    assert query.to_dict() == {"q": "x", "type": "search", "before": "2024-05-01"}
    assert SerperQuery.from_dict(query.to_dict()).before == dt.date(2024, 5, 1)


def test_before_keeps_other_tbs_options():
    query = SerperQuery(q="x", tbs="sbd:1", before="2024-05-01")
    query.validate()
    assert query.request_body()["tbs"] == "sbd:1,cdr:1,cd_min:1/1/1900,cd_max:4/30/2024"


@pytest.mark.parametrize("query, problem", [
    (SerperQuery(q="x", before="2024-13-01"), "must be a date"),
    (SerperQuery(q="x", before="yesterday"), "must be a date"),
    (SerperQuery(q="x", before="2024-05-01", tbs="qdr:w"), "cannot be combined"),
    (SerperQuery(q="x", before="2024-05-01", tbs="cdr:1,cd_max:1/1/2020"),
     "cannot be combined"),
])
def test_before_refuses_what_it_cannot_mean(query, problem):
    with pytest.raises(ValueError, match=problem):
        query.validate()


def test_without_before_the_request_is_unchanged():
    assert "tbs" not in SerperQuery(q="x").request_body()
    assert SerperQuery(q="x", tbs="qdr:d").request_body()["tbs"] == "qdr:d"


# --- Serper: excluded sites ---------------------------------------------------------

def test_excluded_sites_become_site_operators():
    query = SerperQuery(q="gauze gaza", exclude_sites=[
        "snopes.com", "https://www.PolitiFact.com/factchecks/x", "snopes.com"])
    query.validate()
    assert query.excluded_domains == ["snopes.com", "politifact.com"]
    assert query.request_body() == {"q": "gauze gaza -site:snopes.com -site:politifact.com"}
    # What travels to the server is the query as given; the operators are added there
    assert query.to_dict()["q"] == "gauze gaza"


def test_only_as_many_operators_as_google_reads_are_sent():
    terms = " ".join(["word"] * 30)
    query = SerperQuery(q=terms, exclude_sites=["a.com", "b.com", "c.com"])
    assert query.request_body()["q"] == f"{terms} -site:a.com -site:b.com"


@pytest.mark.parametrize("sites", [["not a domain"], ["localhost"], [""], "snopes.com"])
def test_excluded_sites_must_be_domains(sites):
    with pytest.raises(ValueError, match="exclude_sites"):
        SerperQuery(q="x", exclude_sites=sites).validate()


def test_excluded_sites_are_removed_from_the_answer():
    response = SerperResponse.from_dict(sample("serper_search.json"))
    kept = response.without_sites(["wikipedia.org", "yahoo.com"])

    # Subdomains go with their site: en.wikipedia.org, finance.yahoo.com
    assert kept.urls == ["https://www.apple.com/"]
    assert kept.answer_box is None  # It quoted Wikipedia
    assert kept.people_also_ask == []
    assert kept.top_stories == response.top_stories  # Reuters stays
    assert kept.knowledge_graph == response.knowledge_graph
    assert response.urls[1].startswith("https://en.wikipedia.org")  # Not changed in place


def test_excluded_sites_are_removed_from_image_results():
    response = SerperResponse.from_dict(sample("serper_images.json"))
    kept = response.without_sites(["britannica.com"])
    assert [image.source for image in kept.images] == ["Wikipedia", "La tour Eiffel"]


def test_a_domain_is_not_a_suffix_of_another():
    response = SerperResponse(organic=[SerperOrganicResult(link="https://notsnopes.com/x"),
                                       SerperOrganicResult(link="https://snopes.com/y")])
    assert response.without_sites(["snopes.com"]).urls == ["https://notsnopes.com/x"]
