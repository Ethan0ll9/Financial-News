"""供盤前 macro 使用：同一輪 digest 內最後一次 RSS 擷取結果。"""
from __future__ import annotations

from typing import List

from financial_news.models import NewsItem

_rss_digest_items: List[NewsItem] = []


def reset_digest_rss_items() -> None:
    global _rss_digest_items
    _rss_digest_items = []


def set_digest_rss_items(items: List[NewsItem]) -> None:
    global _rss_digest_items
    _rss_digest_items = list(items)


def get_digest_rss_items() -> List[NewsItem]:
    return list(_rss_digest_items)
