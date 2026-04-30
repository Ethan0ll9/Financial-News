"""FinMind API v4 輕量客戶端。"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional

from financial_news.core.api_endpoints import FINMIND_DATA_URL
from financial_news.core.http import HttpClient
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)


@dataclass(frozen=True)
class IndexBar:
    """單日大盤（或指數）OHLCV。"""

    day: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    trading_money: int


class FinMindClient:
    def __init__(self, token: str, *, cache_dir: Optional[Path] = None) -> None:
        self._token = token.strip()
        self._cache_dir = cache_dir
        self._headers = {"Authorization": f"Bearer {self._token}"}
        # 共享 Session：同次盤前/盤後可能呼叫 6~10 次 FinMind，keep-alive 對效能有幫助
        # 預設 timeout=90，部分大 dataset（如 TradingDate）以 per-call 覆寫為 120
        self._http = HttpClient(timeout=90.0, name="finmind")

    def _get(self, params: dict[str, Any], *, timeout: float = 90.0) -> List[dict[str, Any]]:
        if not self._token:
            raise ValueError("FINMIND_TOKEN 未設定")
        payload = self._http.get_json(
            FINMIND_DATA_URL,
            params=params,
            headers=self._headers,
            timeout=timeout,
        )
        status = payload.get("status")
        if status != 200:
            raise RuntimeError(payload.get("msg") or str(payload))
        data = payload.get("data")
        if data is None:
            return []
        if not isinstance(data, list):
            return []
        return data

    def fetch_stock_price_day(
        self,
        stock_id: str,
        start_date: str,
        end_date: str,
    ) -> List[dict[str, Any]]:
        return self._get(
            {
                "dataset": "TaiwanStockPrice",
                "data_id": stock_id,
                "start_date": start_date,
                "end_date": end_date,
            }
        )

    def fetch_trading_dates(self) -> List[str]:
        """回傳 YYYY-MM-DD 交易日列表（由 TaiwanStockTradingDate）。"""
        cache_path = None
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = self._cache_dir / "finmind_trading_dates.json"
            if cache_path.is_file():
                try:
                    raw = json.loads(cache_path.read_text(encoding="utf-8"))
                    mtime = cache_path.stat().st_mtime
                    if time.time() - mtime < 86400 * 7 and isinstance(raw, list):
                        return [str(x) for x in raw]
                except (OSError, json.JSONDecodeError, TypeError):
                    pass

        rows = self._get({"dataset": "TaiwanStockTradingDate"}, timeout=120.0)
        out: List[str] = []
        for row in rows:
            d = row.get("date")
            if d:
                # API 可能回 "2020-01-02" 或帶時間
                ds = str(d)[:10]
                if len(ds) == 10 and ds[4] == "-" and ds[7] == "-":
                    out.append(ds)
        out = sorted(set(out))
        if cache_path and out:
            try:
                cache_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            except OSError as e:
                logger.warning("寫入交易日快取失敗: %s", e)
        return out

    def fetch_stock_info(self) -> List[dict[str, Any]]:
        """台股總覽（產業分類）。"""
        cache_path = None
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = self._cache_dir / "finmind_taiwan_stock_info.json"
            if cache_path.is_file():
                try:
                    mtime = cache_path.stat().st_mtime
                    if time.time() - mtime < 86400 * 3:
                        raw = json.loads(cache_path.read_text(encoding="utf-8"))
                        if isinstance(raw, list) and raw:
                            return raw
                except (OSError, json.JSONDecodeError, TypeError):
                    pass
        rows = self._get({"dataset": "TaiwanStockInfo"}, timeout=120.0)
        if cache_path and rows:
            try:
                cache_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            except OSError as e:
                logger.warning("寫入 StockInfo 快取失敗: %s", e)
        return rows

    def fetch_suspended(self, start_date: str, end_date: str) -> List[dict[str, Any]]:
        return self._get(
            {
                "dataset": "TaiwanStockSuspended",
                "start_date": start_date,
                "end_date": end_date,
            }
        )

    def index_bars_from_price_rows(self, rows: List[dict[str, Any]]) -> List[IndexBar]:
        bars: List[IndexBar] = []
        for row in rows:
            d = str(row.get("date", ""))[:10]
            if len(d) != 10:
                continue
            try:
                bars.append(
                    IndexBar(
                        day=d,
                        open=float(row.get("open") or 0),
                        high=float(row.get("max") or 0),
                        low=float(row.get("min") or 0),
                        close=float(row.get("close") or 0),
                        volume=int(row.get("Trading_Volume") or 0),
                        trading_money=int(row.get("Trading_money") or 0),
                    )
                )
            except (TypeError, ValueError):
                continue
        bars.sort(key=lambda b: b.day)
        return bars

    def latest_index_bar(
        self,
        stock_id: str,
        end_d: date,
        lookback_days: int = 20,
    ) -> Optional[IndexBar]:
        start_d = end_d - timedelta(days=lookback_days)
        rows = self.fetch_stock_price_day(
            stock_id,
            start_d.isoformat(),
            end_d.isoformat(),
        )
        bars = self.index_bars_from_price_rows(rows)
        if not bars:
            return None
        end_s = end_d.isoformat()
        prior = [b for b in bars if b.day <= end_s]
        return prior[-1] if prior else None

    def bars_on_or_before(
        self,
        stock_id: str,
        ref: date,
        *,
        n: int = 5,
        lookback_calendar_days: int = 40,
    ) -> List[IndexBar]:
        """取得 ref 當日或之前最近 n 個有資料的交易日 K 線。"""
        start_d = ref - timedelta(days=lookback_calendar_days)
        rows = self.fetch_stock_price_day(
            stock_id,
            start_d.isoformat(),
            ref.isoformat(),
        )
        bars = self.index_bars_from_price_rows(rows)
        bars = [b for b in bars if b.day <= ref.isoformat()]
        return bars[-n:] if bars else []
