"""TWSE OpenAPI：每日市場成交資訊 FMTQIK。

回傳全市場（含集中市場）的成交股數、成交金額、成交筆數、加權指數收盤、
**漲跌點數**。彌補 FinMind ``TaiwanStockPrice data_id=TAIEX`` 只有 OHLC、
缺成交量／量能與點數的不足。

來源：https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK
範例（民國日期 1150427 → 2026-04-27）：
    {"Date":"1150427","TradeVolume":"14212315987","TradeValue":"1248027623513",
     "Transaction":"6067624","TAIEX":"39616.63","Change":"684.23"}
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

from financial_news.core.api_endpoints import TWSE_FMTQIK_URL as FMTQIK_URL
from financial_news.tw_briefing.exdividend import roc_minguo_date_to_gregorian
from financial_news.tw_briefing.twse_openapi import fetch_twse_list
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)


@dataclass(frozen=True)
class MarketTotals:
    """單一交易日的全市場成交與加權指數摘要。"""

    trade_date: date
    taiex_close: float
    change_pts: float            # 已含正負號（FMTQIK 原始字串）
    trade_volume_shares: int     # 全市場成交股數
    trade_value_yuan: int        # 全市場成交金額（元）
    transaction_count: int       # 成交筆數


from financial_news.tw_briefing.twse_parse import (
    parse_twse_float as _to_float,
    parse_twse_int as _to_int,
)


def _row_to_totals(row: Dict[str, Any]) -> Optional[MarketTotals]:
    d = roc_minguo_date_to_gregorian(str(row.get("Date") or ""))
    if d is None:
        return None
    return MarketTotals(
        trade_date=d,
        taiex_close=_to_float(row.get("TAIEX")),
        change_pts=_to_float(row.get("Change")),
        trade_volume_shares=_to_int(row.get("TradeVolume")),
        trade_value_yuan=_to_int(row.get("TradeValue")),
        transaction_count=_to_int(row.get("Transaction")),
    )


def fetch_fmtqik_all() -> List[Dict[str, Any]]:
    """抓取 TWSE FMTQIK 原始回傳（list[dict]）。失敗回 []。"""
    return fetch_twse_list(FMTQIK_URL)


def parse_fmtqik(rows: List[Dict[str, Any]]) -> List[MarketTotals]:
    out: List[MarketTotals] = []
    for r in rows:
        t = _row_to_totals(r)
        if t is not None:
            out.append(t)
    out.sort(key=lambda x: x.trade_date)
    return out


def market_totals_on(target: date, *, rows: Optional[List[Dict[str, Any]]] = None) -> Optional[MarketTotals]:
    """取目標日期的市場總成交與加權指數收盤；當日無資料回 None。"""
    if rows is None:
        rows = fetch_fmtqik_all()
    for t in parse_fmtqik(rows):
        if t.trade_date == target:
            return t
    return None
