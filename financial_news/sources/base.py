"""新聞來源抽象介面。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from financial_news.models import NewsItem


class NewsSource(ABC):
    """可擴充的新聞來源。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """顯示名稱（LINE 訊息標頭用）。"""

    @abstractmethod
    def fetch_top(self, n: int) -> List[NewsItem]:
        """擷取最多 n 則新聞。"""
