"""Source adapters — anything that produces `CuratedItem` candidates.

Each source implements the `FeedSource` Protocol from `base.py`. The curator
runs them in parallel per tick. A source is free to raise; the curator catches
and logs without killing other sources.
"""

from .base import CuratedCandidate, FeedSource
from .moneyflow_pull import MoneyflowSource
from .naver_news import NaverNewsSource
from .rss_generic import RssGenericSource
from .toss_invest import TossInvestSource
from .x_playwright import XPlaywrightSource

__all__ = [
    "CuratedCandidate",
    "FeedSource",
    "MoneyflowSource",
    "NaverNewsSource",
    "RssGenericSource",
    "TossInvestSource",
    "XPlaywrightSource",
]
