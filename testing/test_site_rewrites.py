"""Addresses that serve their content better in another form, and domains told apart
under a country's public second level (gov.ua)."""

import pytest

from scrapemm.server.util import get_domain, preprocess_url

pytestmark = pytest.mark.server


@pytest.mark.parametrize("url, rewritten", [
    ("https://zakon.rada.gov.ua/laws/show/2108-20#Text", "https://zakon.rada.gov.ua/laws/show/2108-20/print#Text"),
    ("https://zakon.rada.gov.ua/laws/show/998_300", "https://zakon.rada.gov.ua/laws/show/998_300/print"),
    ("https://zakon.rada.gov.ua/laws/show/1697-12/print", "https://zakon.rada.gov.ua/laws/show/1697-12/print"),
    ("https://hudoc.echr.coe.int/fre#%7B%22itemid%22:[%22001-224984%22]%7D",
     "https://hudoc.echr.coe.int/app/conversion/docx/html/body?library=ECHR&id=001-224984"),
    ("https://web.archive.org/web/20250825215446/https:/www.bloomberg.com/news/a",
     "https://web.archive.org/web/20250825215446/https://www.bloomberg.com/news/a"),
    ("https://tgstat.ru/channel/@OdessaRussi/242557", "https://t.me/OdessaRussi/242557"),
    # The query keeps what has a meaning there: "%2B" decoded would be read as a space
    ("https://www.eci.gov.in/api/download?url=a%2Fb%2Bc%3D%3D", "https://www.eci.gov.in/api/download?url=a%2Fb%2Bc%3D%3D"),
    ("https://example.com/a%20b?q=caf%C3%A9", "https://example.com/a b?q=café"),
])
def test_rewrites(url, rewritten):
    assert preprocess_url(url) == rewritten


@pytest.mark.parametrize("url, domain", [
    ("https://zakon.rada.gov.ua/laws", "rada.gov.ua"),
    ("https://www.gp.gov.ua/en", "gp.gov.ua"),
    ("https://www.bbc.co.uk/news", "bbc.co.uk"),
    ("https://historico.tsj.gob.ve/x", "tsj.gob.ve"),
    ("https://pmc.ncbi.nlm.nih.gov/articles/PMC1/", "nih.gov"),
    ("https://t.co/abc", "t.co"),
    ("https://www.reddit.com/r/x", "reddit.com"),
])
def test_domains(url, domain):
    assert get_domain(url) == domain


def test_public_suffix_entries_are_dropped(tmp_path):
    from scrapemm.server.blacklist import DomainBlacklist
    path = tmp_path / "blacklist.yaml"
    path.write_text("gov.ua: CAPTCHA\nexample.com: CAPTCHA\n", encoding="utf-8")
    assert set(DomainBlacklist(path)._load()) == {"example.com"}
