"""自動偵測當日「熱門族群」與「熱門個股」。

不使用 LLM；純以 proxy 清單在該交易日的 FinMind 收盤（close/漲跌幅/成交金額），
依 TWSE 產業分類彙總與排序。輸出結果供 chart、html、flex、text 模組共用。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List

from financial_news.tw_briefing.market_queries import ProxyStat


@dataclass
class ThemeSummary:
    """熱門族群（以 TWSE 產業分類為單位，proxy 範圍內）。"""

    industry: str
    avg_pct: float
    total_turnover: int
    members: List[ProxyStat] = field(default_factory=list)

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def total_turnover_yi(self) -> float:
        return self.total_turnover / 1e8

    def leaders(self, n: int = 5) -> List[ProxyStat]:
        return sorted(self.members, key=lambda s: -s.pct)[:n]


@dataclass
class MarketDigest:
    """當日市場速覽：整體 + 熱門族群 + 熱門個股。供下游視覺化使用。"""

    total_members: int
    advancers: int
    decliners: int
    unchanged: int
    avg_pct: float
    total_turnover: int
    hot_themes: List[ThemeSummary] = field(default_factory=list)
    cold_themes: List[ThemeSummary] = field(default_factory=list)
    top_gainers: List[ProxyStat] = field(default_factory=list)
    top_losers: List[ProxyStat] = field(default_factory=list)
    top_turnover: List[ProxyStat] = field(default_factory=list)

    @property
    def total_turnover_yi(self) -> float:
        return self.total_turnover / 1e8


def _theme_score(t: ThemeSummary) -> float:
    """綜合分數：平均漲幅 + log(成交金額)*權重。避免 1 檔高爆冷門主導。"""
    from math import log1p

    return t.avg_pct + 0.6 * log1p(t.total_turnover_yi)


def build_market_digest(
    stats: List[ProxyStat],
    *,
    hot_themes_k: int = 4,
    cold_themes_k: int = 3,
    top_gainers_n: int = 8,
    top_losers_n: int = 5,
    top_turnover_n: int = 12,
    min_members_per_theme: int = 2,
) -> MarketDigest:
    """依 proxy stats 產出當日摘要。

    - ``hot_themes``：以 _theme_score 排序取 Top k；要求成員 >= ``min_members_per_theme``。
    - ``cold_themes``：以平均漲幅由小到大取 Top k；同樣要求 >= ``min_members_per_theme``。
    - ``top_gainers``/``top_losers``：依當日漲跌幅排序。
    - ``top_turnover``：依當日成交金額排序。
    """
    if not stats:
        return MarketDigest(
            total_members=0,
            advancers=0,
            decliners=0,
            unchanged=0,
            avg_pct=0.0,
            total_turnover=0,
        )

    from financial_news.tw_briefing.market_breadth import _normalize_industry

    by_ind: Dict[str, List[ProxyStat]] = defaultdict(list)
    for s in stats:
        by_ind[_normalize_industry(s.industry)].append(s)

    themes_all: List[ThemeSummary] = []
    for ind, lst in by_ind.items():
        themes_all.append(
            ThemeSummary(
                industry=ind,
                avg_pct=sum(x.pct for x in lst) / len(lst),
                total_turnover=sum(x.turnover for x in lst),
                members=list(lst),
            )
        )

    themes_multi = [t for t in themes_all if t.member_count >= min_members_per_theme]
    base_pool = themes_multi or themes_all

    # 熱門族群：平均漲幅 > 0（or all non-negative if none positive），再依綜合分數排序
    pos_pool = [t for t in base_pool if t.avg_pct > 0]
    if not pos_pool:
        pos_pool = [t for t in base_pool if t.avg_pct >= 0] or base_pool
    hot_themes = sorted(pos_pool, key=lambda t: -_theme_score(t))[:hot_themes_k]

    # 偏弱族群：從整池挑最弱
    cold_themes = sorted(base_pool, key=lambda t: t.avg_pct)[:cold_themes_k]

    top_gainers = sorted(stats, key=lambda s: -s.pct)[:top_gainers_n]
    top_losers = sorted(stats, key=lambda s: s.pct)[:top_losers_n]
    top_turnover = sorted(stats, key=lambda s: -s.turnover)[:top_turnover_n]

    advancers = sum(1 for s in stats if s.pct > 0)
    decliners = sum(1 for s in stats if s.pct < 0)
    unchanged = len(stats) - advancers - decliners
    avg_pct = sum(s.pct for s in stats) / len(stats)
    total_turnover = sum(s.turnover for s in stats)

    return MarketDigest(
        total_members=len(stats),
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
        avg_pct=avg_pct,
        total_turnover=total_turnover,
        hot_themes=hot_themes,
        cold_themes=cold_themes,
        top_gainers=top_gainers,
        top_losers=top_losers,
        top_turnover=top_turnover,
    )
