"""資料模型。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class NewsItem:
    """單則新聞（供格式化與推播）。"""

    title: str
    url: str
    source_label: str
