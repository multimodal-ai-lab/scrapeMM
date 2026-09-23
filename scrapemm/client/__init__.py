"""The scrapeMM client. This is what `pip install scrapeMM` gives you."""

from .client import retrieve
from .settings import Settings, configure, settings

__all__ = ["retrieve", "configure", "settings", "Settings"]
