"""RSS 訂閱來源。"""
from __future__ import annotations

from typing import List
from urllib.parse import urlparse

import feedparser
import requests

from financial_news.models import NewsItem
from financial_news.sources.base import NewsSource
from financial_news.utils import setup_logger

logger = setup_logger(__name__)

HEADERS = {
    "User-Agent": "Financial-NewsDigest/1.0 (+https://github.com/)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def _feed_label(url: str) -> str:
    host = urlparse(url).netloc or url
    return f"RSS（{host}）"


class RssFeedSource(NewsSource):
    """從一或多個 RSS feed 合併後取前 n 則。"""

    def __init__(self, feed_urls: List[str], timeout_sec: float = 30.0) -> None:
        self._feed_urls = list(feed_urls)
        self._timeout = timeout_sec

    @property
    def name(self) -> str:
        if len(self._feed_urls) == 1:
            return _feed_label(self._feed_urls[0])
        return f"RSS（{len(self._feed_urls)} 個 feed）"

    def fetch_top(self, n: int) -> List[NewsItem]:
        if n <= 0 or not self._feed_urls:
            return []
        merged: List[NewsItem] = []
        for url in self._feed_urls:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=self._timeout)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
            except requests.RequestException as e:
                logger.error("RSS 請求失敗 %s: %s", url, e)
                continue
            if getattr(parsed, "bozo", False) and not parsed.entries:
                logger.warning("RSS 解析可能有誤 %s: %s", url, getattr(parsed, "bozo_exception", ""))

            label = _feed_label(url)
            for entry in parsed.entries:
                if len(merged) >= n:
                    return merged[:n]
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                if not title or not link:
                    continue
                merged.append(NewsItem(title=title, url=link, source_label=label))
        return merged[:n]
