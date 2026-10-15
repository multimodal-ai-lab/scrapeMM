"""ResearchGate turns this server away; its publications come from OpenAlex instead."""

import pytest

from scrapemm.server.integrations.researchgate import _abstract, _describe, _similarity

pytestmark = pytest.mark.server

WORK = {"id": "https://openalex.org/W1", "title": "The benefit of Indian jaggery over sugar on human health",
        "publication_date": "2020-01-01", "doi": "https://doi.org/10.1016/b978-0-12-816918-6.00016-0",
        "authorships": [{"author": {"display_name": "A. Author"}}],
        "abstract_inverted_index": {"Jaggery": [0], "is": [1], "sweet.": [2]}}


def test_titles_match_regardless_of_punctuation():
    assert _similarity("Biomedical Applications of Black Pepper the King of Spices A Review",
                       '"Biomedical Applications of Black Pepper, the King of Spices: A Review"') > 0.95
    assert _similarity("The benefit of Indian jaggery over sugar on human health",
                       "The health halo of jaggery: Benefits beyond sugar") < 0.9


def test_the_record_says_where_it_is_from():
    assert _abstract(WORK) == "Jaggery is sweet."
    text = _describe(WORK, "https://www.researchgate.net/publication/339908838_The_benefit")
    assert "this is the paper's record in OpenAlex" in text and "Authors: A. Author" in text
    assert text.endswith("Abstract:\nJaggery is sweet.")

