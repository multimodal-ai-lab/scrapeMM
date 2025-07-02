import logging
import sys
from pathlib import Path

import yaml

from scrapemm.util import get_domain

logger = logging.getLogger("Retriever")
logger.setLevel(logging.DEBUG)

# Only add handler if none exists (avoid duplicate logs on rerun)
if not logger.hasHandlers():
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('[%(levelname)s]: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def is_no_bot_site(url: str) -> bool:
    """Checks if the URL belongs to a known unsupported website."""
    domain = get_domain(url)
    return domain is None or domain.endswith(".gov") or domain in no_bot_domains


def read_urls_from_file(file_path):
    with open(file_path, 'r') as f:
        return f.read().splitlines()


def save_to_config(dictionary: dict):
    config.update(dictionary)
    yaml.dump(config, open(CONFIG_PATH, "w"))


no_bot_domains_file = Path(__file__).parent / "no_bot_domains.txt"
no_bot_domains = read_urls_from_file(no_bot_domains_file)

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
config = yaml.safe_load(open(CONFIG_PATH))
firecrawl_url = config.get("firecrawl_url")
