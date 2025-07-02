import sys
from pathlib import Path

from scrapemm.util import get_domain
import logging

logger = logging.getLogger("Retriever")
logger.setLevel(logging.DEBUG)

# Only add handler if none exists (avoid duplicate logs on rerun)
if not logger.hasHandlers():
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('[%(levelname)s]: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def is_unsupported_site(url: str) -> bool:
    """Checks if the URL belongs to a known unsupported website."""
    domain = get_domain(url)
    return domain is None or domain.endswith(".gov") or domain in unsupported_domains


def read_urls_from_file(file_path):
    with open(file_path, 'r') as f:
        return f.read().splitlines()


unsupported_domains_file = Path(__file__).parent / "unsupported_domains.txt"
unsupported_domains = read_urls_from_file(unsupported_domains_file)
