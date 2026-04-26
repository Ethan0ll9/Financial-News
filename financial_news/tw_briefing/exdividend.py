"""除權息等：TWSE 公開 API TWT48U_ALL（免金鑰）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, List, Optional

import requests

from financial_news.utils import setup_logger

logger = setup_logger(__name__)

TWT48U_URL = "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"


@dataclass(frozen=True)
class ExDividendEvent:
    """除權／除息／現增等預告（來源：TWSE OpenAPI）。"""

    ex_date: date
    stock_id: str
    stock_name: str
    note: str


def roc_minguo_date_to_gregorian(s: str) -> Optional[date]:
    """TWSE 民國日期字串 YYYMMDD（7 碼）轉西元 date。"""
    raw = str(s).strip()
    if len(raw) != 7 or not raw.isdigit():
        return None
    roc_y = int(raw[:3])
    month = int(raw[3:5])
    day = int(raw[5:7])
    try:
        return date(roc_y + 1911, month, day)
    except ValueError:
        return None


def fetch_twt48u_all() -> List[dict[str, Any]]:
    try:
        resp = requests.get(TWT48U_URL, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return []
    except (requests.RequestException, ValueError) as e:
        logger.warning("TWT48U_ALL 擷取失敗: %s", e)
        return []


def _row_to_event(row: dict[str, Any]) -> Optional[ExDividendEvent]:
    ex_d = roc_minguo_date_to_gregorian(str(row.get("Date") or ""))
    if ex_d is None:
        return None
    code = str(row.get("Code") or "").strip()
    name = str(row.get("Name") or "").strip()
    ex_type = str(row.get("Exdividend") or "").strip()
    cash = str(row.get("CashDividend") or "").strip()
    parts = [x for x in (ex_type, f"現金{cash}" if cash else "") if x]
    note = "／".join(parts) if parts else "除權息預告"
    if not code:
        return None
    return ExDividendEvent(ex_date=ex_d, stock_id=code, stock_name=name, note=note)


def events_on_date(all_rows: Optional[List[dict[str, Any]]], target: date) -> List[ExDividendEvent]:
    rows = all_rows if all_rows is not None else fetch_twt48u_all()
    out: List[ExDividendEvent] = []
    for row in rows:
        ev = _row_to_event(row)
        if ev and ev.ex_date == target:
            out.append(ev)
    return out


def events_in_date_range(
    all_rows: Optional[List[dict[str, Any]]],
    start: date,
    end: date,
) -> List[ExDividendEvent]:
    rows = all_rows if all_rows is not None else fetch_twt48u_all()
    out: List[ExDividendEvent] = []
    for row in rows:
        ev = _row_to_event(row)
        if ev and start <= ev.ex_date <= end:
            out.append(ev)
    out.sort(key=lambda e: (e.ex_date, e.stock_id))
    return out
