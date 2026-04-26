"""停復牌等：FinMind TaiwanStockSuspended（若權限不足則降級為空）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional

from financial_news.tw_briefing.finmind_client import FinMindClient
from financial_news.utils import setup_logger

logger = setup_logger(__name__)


@dataclass(frozen=True)
class SuspendedEvent:
    stock_id: str
    announce_date: str
    suspension_time: str
    resumption_date: str
    resumption_time: str


def parse_suspended_row(row: dict) -> Optional[SuspendedEvent]:
    sid = str(row.get("stock_id") or "").strip()
    if not sid:
        return None
    return SuspendedEvent(
        stock_id=sid,
        announce_date=str(row.get("date") or "")[:10],
        suspension_time=str(row.get("suspension_time") or ""),
        resumption_date=str(row.get("resumption_date") or "")[:10],
        resumption_time=str(row.get("resumption_time") or ""),
    )


def fetch_suspended_between(
    client: FinMindClient,
    start: date,
    end: date,
) -> List[SuspendedEvent]:
    try:
        rows = client.fetch_suspended(start.isoformat(), end.isoformat())
    except Exception as e:
        logger.warning("TaiwanStockSuspended 擷取失敗（可能需贊助權限）: %s", e)
        return []
    out: List[SuspendedEvent] = []
    for row in rows:
        ev = parse_suspended_row(row)
        if ev:
            try:
                d = datetime.strptime(ev.announce_date[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if start <= d <= end:
                out.append(ev)
    out.sort(key=lambda x: (x.announce_date, x.stock_id))
    return out
