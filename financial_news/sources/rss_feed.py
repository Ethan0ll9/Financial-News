"""RSS 訂閱來源。"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlparse

import feedparser
import requests

from financial_news.models import NewsItem
from financial_news.sources.base import NewsSource
from financial_news.sources.rss_catalog import rss_feed_meta
from financial_news.utils import setup_logger, strip_html

logger = setup_logger(__name__)

HEADERS = {
    "User-Agent": "Financial-NewsDigest/1.0 (+https://github.com/)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def _feed_label(url: str) -> str:
    host = urlparse(url).netloc or url
    return f"RSS（{host}）"


class RssFeedSource(NewsSource):
    """多個 RSS：每家 feed 各取最多 `items_per_feed` 則，依 URL 順序串接。

    `fetch_top` 的參數 n 來自排程（TOP_N），此來源會忽略 n，改以 `items_per_feed` 為準。
    """

    def __init__(
        self,
        feed_urls: List[str],
        *,
        items_per_feed: int = 5,
        max_total_items: Optional[int] = None,
        timeout_sec: float = 30.0,
    ) -> None:
        self._feed_urls = list(feed_urls)
        self._items_per_feed = max(1, items_per_feed)
        self._max_total_items = max_total_items
        self._timeout = timeout_sec

    @property
    def name(self) -> str:
        if len(self._feed_urls) == 1:
            return _feed_label(self._feed_urls[0])
        suffix = f"，各最多 {self._items_per_feed} 則"
        if self._max_total_items is not None:
            suffix += f"，合併上限 {self._max_total_items} 則"
        return f"RSS（{len(self._feed_urls)} 個 feed{suffix}）"

    def _fetch_one_feed(self, url: str, max_entries: int) -> List[NewsItem]:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=self._timeout)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except requests.RequestException as e:
            logger.error("RSS 請求失敗 %s: %s", url, e)
            return []
        if getattr(parsed, "bozo", False) and not parsed.entries:
            logger.warning(
                "RSS 解析可能有誤 %s: %s",
                url,
                getattr(parsed, "bozo_exception", ""),
            )

        region, outlet, source_label = rss_feed_meta(url)
        out: List[NewsItem] = []
        for entry in parsed.entries[:max_entries]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            out.append(
                NewsItem(
                    title=strip_html(title),
                    url=link,
                    source_label=source_label,
                    region=region,
                    outlet=outlet,
                )
            )
        return out

    def fetch_top(self, _n: int) -> List[NewsItem]:
        # 使用 items_per_feed；與 TOP_N（鉅亨）分開設定
        if not self._feed_urls:
            return []
        cap = self._items_per_feed
        merged: List[NewsItem] = []
        for url in self._feed_urls:
            items = self._fetch_one_feed(url, max_entries=cap)
            merged.extend(items[:cap])
        if self._max_total_items is not None:
            return merged[: self._max_total_items]
        return merged
