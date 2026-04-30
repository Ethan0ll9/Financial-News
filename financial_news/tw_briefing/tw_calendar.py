"""台股交易日：以 FinMind TaiwanStockTradingDate 為準（本地快取）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import FrozenSet, List, Optional, Set

from financial_news.tw_briefing.finmind_client import FinMindClient


def _parse_iso(d: str) -> Optional[date]:
    try:
        return datetime.strptime(d[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass
class TwCalendar:
    """交易日集合與查詢。"""

    trading_days: FrozenSet[date]

    @classmethod
    def from_finmind(cls, client: FinMindClient) -> "TwCalendar":
        raw = client.fetch_trading_dates()
        ds: Set[date] = set()
        for s in raw:
            pd = _parse_iso(s)
            if pd:
                ds.add(pd)
        return cls(frozenset(ds))

    def is_trading_day(self, d: date) -> bool:
        return d in self.trading_days

    def previous_trading_day(self, ref: date) -> Optional[date]:
        d = ref
        for _ in range(40):
            d = d - timedelta(days=1)
            if d in self.trading_days:
                return d
        return None

    def next_trading_day(self, ref: date) -> Optional[date]:
        d = ref
        for _ in range(40):
            d = d + timedelta(days=1)
            if d in self.trading_days:
                return d
        return None

    def next_n_trading_days(self, ref: date, n: int) -> List[date]:
        """從 ref 之後（不含 ref 本身）數出 n 個交易日；不足則回傳實際可達的數量。"""
        out: List[date] = []
        if n <= 0:
            return out
        d = ref
        for _ in range(n * 5):  # 假設最多 5 倍（連假等）
            d = d + timedelta(days=1)
            if d in self.trading_days:
                out.append(d)
                if len(out) >= n:
                    return out
        return out

    def week_trading_days(self, ref: date) -> List[date]:
        """該曆週（週一～週五）內的交易日，且日期 >= ref（用於週一列出本週剩餘）。"""
        monday = ref - timedelta(days=ref.weekday())
        fri = monday + timedelta(days=4)
        out: List[date] = []
        cur = monday
        while cur <= fri:
            if cur >= ref and self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def week_trading_days_full_week(self, ref: date) -> List[date]:
        """該曆週（週一～週五）所有交易日（週一總表用）。"""
        monday = ref - timedelta(days=ref.weekday())
        fri = monday + timedelta(days=4)
        out: List[date] = []
        cur = monday
        while cur <= fri:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out
