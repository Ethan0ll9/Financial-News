"""鉅亨網 popular API。"""
from __future__ import annotations

from typing import Any, List

import requests

from financial_news.core.api_endpoints import (
    CNYES_ARTICLE_URL_TEMPLATE as DEFAULT_ARTICLE_URL_TEMPLATE,
    CNYES_POPULAR_URL,
)
from financial_news.core.http import HttpClient
from financial_news.models import NewsItem
from financial_news.sources.base import NewsSource
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)

HEADERS = {
    "User-Agent": "Financial-NewsDigest/1.0 (+https://github.com/)",
    "Accept": "application/json",
}


class CnyesPopularSource(NewsSource):
    """從鉅亨 popular API 取得熱門列表。"""

    def __init__(self, category: str = "all", timeout_sec: float = 30.0) -> None:
        self._category = category
        self._timeout = timeout_sec
        # 共享 Session：每天 2 次 digest 都會抓；UA/Accept 設為 default headers
        self._http = HttpClient(
            timeout=timeout_sec,
            default_headers=HEADERS,
            name="cnyes",
        )

    @property
    def name(self) -> str:
        return f"鉅亨網（{self._category}）"

    def fetch_top(self, n: int) -> List[NewsItem]:
        if n <= 0:
            return []
        try:
            data = self._http.get_json(CNYES_POPULAR_URL)
        except requests.RequestException as e:
            logger.error("鉅亨 API 請求失敗: %s", e)
            return []
        except ValueError as e:
            logger.error("鉅亨 API 回傳非 JSON: %s", e)
            return []

        status = data.get("statusCode")
        if status != 200:
            logger.error("鉅亨 API statusCode=%s message=%s", status, data.get("message"))
            return []

        items: Any = data.get("items") or {}
        if not isinstance(items, dict):
            logger.error("鉅亨 API items 格式異常")
            return []

        raw_list = items.get(self._category)
        if not isinstance(raw_list, list):
            logger.warning("鉅亨 API 無 category=%s 或型別非陣列，改用 all", self._category)
            raw_list = items.get("all")
            if not isinstance(raw_list, list):
                return []

        out: List[NewsItem] = []
        for row in raw_list[:n]:
            if not isinstance(row, dict):
                continue
            news_id = row.get("newsId")
            title = row.get("title")
            if title is None or news_id is None:
                continue
            link = row.get("newsUrl") or row.get("url")
            if not link:
                link = DEFAULT_ARTICLE_URL_TEMPLATE.format(news_id=news_id)
            out.append(
                NewsItem(
                    title=str(title).strip(),
                    url=str(link).strip(),
                    source_label=self.name,
                    # 鉅亨網本身為台灣媒體；標上 region 讓 digest 過濾邏輯
                    # 可判斷為台媒並保留（避免被 LINE 精簡規則濾掉）。
                    region="🇹🇼 台灣",
                    outlet=f"鉅亨網（{self._category}）",
                )
            )
        return out
