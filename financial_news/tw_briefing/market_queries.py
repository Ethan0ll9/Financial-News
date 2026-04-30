"""FinMind 共用查詢：K 線、代號對照、族群／成交統計與格式化。"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from financial_news.tw_briefing.finmind_client import FinMindClient, IndexBar
from financial_news.tw_briefing.market_text import pct_change
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)


def stock_bars(client: FinMindClient, stock_id: str, end: date, n: int = 2) -> List[IndexBar]:
    return client.bars_on_or_before(stock_id, end, n=n, lookback_calendar_days=45)


def has_bar_on(bars: List[IndexBar], ref: date) -> bool:
    """檢查 bars 最後一筆是否就是 ``ref`` 當日（用於嚴格驗日）。"""
    if not bars:
        return False
    last_day = (bars[-1].day or "").strip()
    return last_day == ref.isoformat()


def weighted_index_bars(
    client: FinMindClient,
    configured_id: str,
    ref: date,
    *,
    n: int = 2,
    strict_today: bool = False,
) -> tuple[List[IndexBar], str]:
    """加權指數日線（TaiwanStockPrice）。

    FinMind 對 ``IX0001`` 常回空列，實務請用 ``TAIEX``；此處先試 env 設定再試 ``TAIEX``。

    ``strict_today=True`` 時，只接受最後一筆 ``day == ref`` 的資料；否則回傳空列，避免
    在 FinMind 當日資料尚未公布（每天 17:30）時，誤把上一交易日的 K 線當作今日報告。
    """
    configured = (configured_id or "").strip()
    candidates: List[str] = []
    for sid in (configured, "TAIEX"):
        if sid and sid not in candidates:
            candidates.append(sid)
    if not candidates:
        candidates = ["TAIEX"]

    first = True
    last_seen_bars: List[IndexBar] = []
    last_seen_sid = candidates[0]
    for sid in candidates:
        bars = stock_bars(client, sid, ref, n=n)
        if bars:
            if not first:
                logger.info(
                    "加權指數改用 FinMind TaiwanStockPrice data_id=%s（%s 無資料）",
                    sid,
                    configured or "（未設定）",
                )
            if strict_today and not has_bar_on(bars, ref):
                logger.warning(
                    "加權指數 %s 最近一筆為 %s，非今日 %s；strict_today 模式下視為未公布",
                    sid,
                    bars[-1].day,
                    ref.isoformat(),
                )
                last_seen_bars = bars
                last_seen_sid = sid
                first = False
                continue
            return bars, sid
        first = False
    if strict_today:
        return [], candidates[0]
    return last_seen_bars, last_seen_sid


@dataclass(frozen=True)
class StockMeta:
    """TaiwanStockInfo：股票名稱與產業。"""

    stock_id: str
    name: str
    industry: str


def stock_meta_map(client: FinMindClient) -> Dict[str, StockMeta]:
    rows = client.fetch_stock_info()
    m: Dict[str, StockMeta] = {}
    for row in rows:
        sid = str(row.get("stock_id") or "").strip()
        if not sid or sid in m:
            continue
        name = str(row.get("stock_name") or "").strip()
        ind = str(row.get("industry_category") or "").strip() or "其他"
        m[sid] = StockMeta(stock_id=sid, name=name, industry=ind)
    return m


def industry_map(client: FinMindClient) -> Dict[str, str]:
    """相容舊呼叫。內部改走 stock_meta_map。"""
    return {sid: m.industry for sid, m in stock_meta_map(client).items()}


@dataclass(frozen=True)
class ProxyStat:
    """單一 proxy 股票在某參考日的快照：收盤、當日漲跌幅、成交金額、名稱產業。"""

    stock_id: str
    name: str
    industry: str
    close: float
    pct: float
    turnover: int

    @property
    def label(self) -> str:
        """顯示「2330 台積電」；若無 name 則僅顯示代號。"""
        return f"{self.stock_id} {self.name}" if self.name else self.stock_id


def _one_proxy_stat(
    client: FinMindClient,
    sid: str,
    ref: date,
    meta_map: Dict[str, StockMeta],
    *,
    strict_today: bool = False,
) -> Optional[ProxyStat]:
    bars = stock_bars(client, sid, ref, n=2)
    if not bars:
        return None
    if strict_today and not has_bar_on(bars, ref):
        return None
    b = bars[-1]
    p: Optional[float] = None
    if len(bars) >= 2 and bars[-2].close > 0:
        p = pct_change(bars[-2].close, b.close)
    meta = meta_map.get(sid)
    return ProxyStat(
        stock_id=sid,
        name=(meta.name if meta else ""),
        industry=(meta.industry if meta else "其他"),
        close=float(b.close or 0.0),
        pct=float(p if p is not None else 0.0),
        turnover=int(b.trading_money or 0),
    )


def gather_proxy_stats(
    client: FinMindClient,
    ref: date,
    proxy_ids: List[str],
    meta_map: Dict[str, StockMeta],
    *,
    max_workers: int = 6,
    strict_today: bool = False,
) -> List[ProxyStat]:
    """對 proxy 清單平行抓取當日／T-1 K 線，彙整為 ProxyStat 列表（已濾除無資料者）。

    ``strict_today=True``：僅保留最後一筆 K 線 day == ref 的 proxy；其他全部丟棄。
    """
    if not proxy_ids:
        return []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(
            ex.map(
                lambda s: _one_proxy_stat(
                    client, s, ref, meta_map, strict_today=strict_today
                ),
                proxy_ids,
            )
        )
    return [r for r in results if r is not None]


from financial_news.tw_briefing.formatting import (
    fmt_pct as _fmt_pct,
    fmt_yi as _fmt_yi_base,
)


def _fmt_turnover_yi(money: int) -> str:
    """成交金額轉「X.XX 億」字串（market_queries 使用 2 位小數）。"""
    return _fmt_yi_base(money, decimals=2)


def format_sector_strength(
    stats: List[ProxyStat],
    *,
    ref: date,
    k: int = 3,
    leaders_per_industry: int = 3,
    min_stocks_per_industry: int = 2,
) -> str:
    """產業強弱 + 每類領漲／領跌代表股（代號＋名稱＋漲跌幅）。

    ``min_stocks_per_industry``：僅 1 檔 proxy 的產業會被排除於強／弱排序（避免單檔支配）；
    若全數產業都不足，會自動降階為 1 檔 fallback，並在輸出註記。
    """
    by_ind: Dict[str, List[ProxyStat]] = defaultdict(list)
    for s in stats:
        by_ind[s.industry].append(s)

    def _avg_by(threshold: int) -> Dict[str, float]:
        return {
            ind: sum(x.pct for x in lst) / len(lst)
            for ind, lst in by_ind.items()
            if len(lst) >= threshold
        }

    ind_avg = _avg_by(min_stocks_per_industry)
    fallback = False
    if not ind_avg and min_stocks_per_industry > 1:
        ind_avg = _avg_by(1)
        fallback = True
    if not ind_avg:
        return "【族群強弱】\n\n（資料不足，無法估算產業平均）"

    strong = sorted(ind_avg.items(), key=lambda x: -x[1])[:k]
    weak = sorted(ind_avg.items(), key=lambda x: x[1])[:k]

    def _fmt_leaders(ind: str, order: str) -> str:
        rows = list(by_ind[ind])
        if order == "strong":
            rows.sort(key=lambda s: -s.pct)
        else:
            rows.sort(key=lambda s: s.pct)
        chosen = rows[:leaders_per_industry]
        return "、".join(f"{s.label} {_fmt_pct(s.pct)}" for s in chosen)

    def _block(title: str, pairs: List[Tuple[str, float]], order: str) -> List[str]:
        lines = [title]
        if not pairs:
            lines.append("（資料不足）")
            return lines
        for ind, avg in pairs:
            leaders_txt = _fmt_leaders(ind, order=order)
            count = len(by_ind[ind])
            lines.append(f"・{ind}（均 {_fmt_pct(avg)}；取樣 {count} 檔）")
            if leaders_txt:
                lines.append(f"  代表：{leaders_txt}")
        return lines

    header_note = ""
    if fallback:
        header_note = "；取樣不足，已降階含單檔產業"
    lines: List[str] = [
        f"【族群強弱（FinMind 產業分類；proxy {len(stats)} 檔；每產業至少 "
        f"{1 if fallback else min_stocks_per_industry} 檔{header_note}；{ref}）】",
        "",
    ]
    lines.extend(_block("▲ 偏強", strong, "strong"))
    lines.append("")
    lines.extend(_block("▼ 偏弱", weak, "weak"))

    single_indies = [
        (ind, lst[0]) for ind, lst in by_ind.items()
        if len(lst) == 1 and (not fallback)
    ]
    if single_indies and not fallback:
        single_indies.sort(key=lambda x: -x[1].pct)
        lines.append("")
        lines.append("（單檔代表；未列入強弱排序）")
        for ind, s in single_indies:
            lines.append(f"・{ind}：{s.label} {_fmt_pct(s.pct)}")
    return "\n".join(lines)


def format_turnover_detail(
    stats: List[ProxyStat],
    *,
    ref: date,
    top_n: int,
) -> str:
    """成交活躍：代號＋名稱＋產業＋漲跌幅＋收盤＋成交金額。"""
    ranked = sorted(stats, key=lambda s: -s.turnover)[:top_n]
    lines = [f"【成交活躍 Top {top_n}（proxy 清單內排序；非全市場；{ref}）】", ""]
    if not ranked:
        lines.append("（無可用成交資料）")
        return "\n".join(lines)
    for i, s in enumerate(ranked, 1):
        lines.append(f"{i}. {s.label}（{s.industry}）")
        lines.append(
            f"   成交 {_fmt_turnover_yi(s.turnover)}｜收 {s.close:,.2f}｜{_fmt_pct(s.pct)}"
        )
    return "\n".join(lines)


def format_movers(
    stats: List[ProxyStat],
    *,
    ref: date,
    top_m: int = 5,
) -> str:
    """振幅／漲跌最顯著（依 |%|）：作為「權值／關注股概況（概略敘述）」。"""
    ranked = sorted(stats, key=lambda s: -abs(s.pct))[:top_m]
    lines = [f"【漲跌顯著股（proxy 絕對漲跌幅；{ref}）】", ""]
    if not ranked:
        lines.append("（資料不足）")
        return "\n".join(lines)
    for s in ranked:
        lines.append(
            f"・{s.label}（{s.industry}）{_fmt_pct(s.pct)}｜成交 {_fmt_turnover_yi(s.turnover)}"
        )
    return "\n".join(lines)
