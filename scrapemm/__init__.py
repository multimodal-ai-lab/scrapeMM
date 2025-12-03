import logging
import sys

import scrapemm.common
from .common import APP_NAME
from .integrations import Telegram, X
from .retrieval import retrieve
from .secrets import configure_secrets

# Set up logger
logger = logging.getLogger(APP_NAME)
logger.setLevel(logging.DEBUG)

# Only add handler if none exists (avoid duplicate logs on rerun)
if not logger.hasHandlers():
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('[%(levelname)s]: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
