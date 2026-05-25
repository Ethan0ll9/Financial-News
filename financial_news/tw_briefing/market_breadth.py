"""全市場行情聚合：TWSE STOCK_DAY_ALL + TPEX daily_close_quotes + TWSE MI_INDEX。

提供：
  - ``fetch_twse_quotes()`` / ``fetch_tpex_quotes()``：全市場個股當日 OHLCV/Change
  - ``fetch_mi_index()``：四大指數收盤＋漲跌（無家數）
  - ``build_index_summary(...)``：四大指數 + 漲跌家數（由 quotes 聚合）
  - ``build_industry_flow(...)``：產業資金淨流入／流出（依 avg_pct 正負分流）

設計上盡量在本模組內做 robust parsing（Change 欄會含「除息」「除權」等文字），
失敗的列直接丟棄、不拋例外，避免單筆髒資料中斷整批。
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from financial_news.core.api_endpoints import (
    TPEX_DAILY_CLOSE_QUOTES_URL,
    TWSE_COMPANY_INFO_URL,
    TWSE_MI_INDEX_URL,
    TWSE_STOCK_DAY_ALL_URL,
)
from financial_news.core.http import HttpClient
from financial_news.core.utils import setup_logger
from financial_news.tw_briefing.market_queries import StockMeta

logger = setup_logger(__name__)


# 「電子工業類指數」成分產業（TaiwanStockInfo 的 industry_category 實際命名）
ELECTRONIC_INDUSTRIES: frozenset[str] = frozenset(
    {
        "半導體業",
        "電腦及週邊設備業",
        "光電業",
        "通信網路業",
        "電子零組件業",
        "電子通路業",
        "資訊服務業",
        "其他電子業",
        "其他電子類",
        "電子工業",
        "電子商務業",
    }
)

# 金融保險類（TaiwanStockInfo 的命名是「金融保險」「金融業」，不是「金融保險業」）
FINANCIAL_INDUSTRIES: frozenset[str] = frozenset({"金融保險", "金融業"})


# MI_INDEX 中文標題對應的「指數名稱」鍵
_MI_KEYS = {
    "name": "指數",
    "close": "收盤指數",
    "sign": "漲跌",
    "pts": "漲跌點數",
    "pct": "漲跌百分比",
}

# 我們關心的 4 個指數（MI_INDEX 中對應的中文 name）
TAIEX_NAME = "發行量加權股價指數"
TPEX_NAME = "櫃買指數"  # MI_INDEX 不含 OTC，改從 quotes/TPEX 聚合
ELECTRONIC_INDEX_NAME = "電子工業類指數"
FINANCIAL_INDEX_NAME = "金融保險類指數"


# ---- 資料結構 ---------------------------------------------------------------


@dataclass(frozen=True)
class StockQuote:
    """單一上市/上櫃股票的當日行情快照（normalize 後）。"""

    stock_id: str
    name: str
    close: float
    change: float  # 漲跌價（含正負號）
    pct: float  # 漲跌幅 %（含正負號）
    turnover: int  # 成交金額（元）
    market: str  # "TWSE" / "TPEX"
    shares_outstanding: int = 0  # 已發行股數（推算市值用）

    @property
    def market_cap(self) -> int:
        """市值（元）≈ 收盤價 × 已發行股數。當已發行股數未知時回 0。"""
        if self.shares_outstanding <= 0:
            return 0
        return int(self.close * self.shares_outstanding)


@dataclass(frozen=True)
class IndexQuote:
    """4 大指數的當日快照 + 漲跌家數。"""

    label: str  # 顯示用："加權指數" / "櫃買指數" / "電子指數" / "金融指數"
    close: float
    diff: float  # 漲跌點數（含正負號；上漲為正、下跌為負）
    pct: float  # 漲跌百分比（含正負號）
    advancers: int
    decliners: int
    unchanged: int

    @property
    def total(self) -> int:
        return self.advancers + self.decliners + self.unchanged


@dataclass(frozen=True)
class IndustryFlow:
    """單一產業當日聚合：總成交金額、總市值、平均漲跌幅。"""

    industry: str
    turnover: int  # 元
    market_cap: int  # 元（成份股市值總和）
    avg_pct: float  # %
    member_count: int


# ---- HTTP 抓取 --------------------------------------------------------------


class MarketBreadthClient:
    def __init__(self, *, http: Optional[HttpClient] = None) -> None:
        self._http = http or HttpClient(timeout=30.0, name="market_breadth")

    def fetch_twse_company_info(self) -> Dict[str, int]:
        """TWSE 上市公司基本資料 → stock_id → 已發行股數 map。

        資料源：``opendata/t187ap03_L``，欄位「已發行普通股股數加TDR發行股數」。
        若該欄缺失則 fallback 用「實收資本額」÷ 面額 10 元估算。
        """
        rows = self._http.get_json(TWSE_COMPANY_INFO_URL, params=None)
        out: Dict[str, int] = {}
        if not isinstance(rows, list):
            return out
        for row in rows:
            sid = str(row.get("公司代號") or "").strip()
            if not _is_common_stock_id(sid):
                continue
            shares_txt = row.get("已發行普通股股數加TDR發行股數")
            shares = _to_int(shares_txt)
            if not shares:
                capital = _to_int(row.get("實收資本額"))
                if capital:
                    shares = capital // 10  # 面額 10 元
            if shares and shares > 0:
                out[sid] = shares
        logger.info("TWSE company info: %d 筆 → 已發行股數對照 %d 檔", len(rows), len(out))
        return out

    def fetch_twse_quotes(
        self,
        *,
        shares_map: Optional[Dict[str, int]] = None,
    ) -> List[StockQuote]:
        """TWSE STOCK_DAY_ALL → 上市個股 list。可選傳入 shares_map 以填入 ``shares_outstanding``。"""
        rows = self._http.get_json(TWSE_STOCK_DAY_ALL_URL, params=None)
        out: List[StockQuote] = []
        if not isinstance(rows, list):
            return out
        shares_map = shares_map or {}
        for row in rows:
            sid = str(row.get("Code") or "").strip()
            if not _is_common_stock_id(sid):
                continue
            close = _to_float(row.get("ClosingPrice"))
            change = _to_float(row.get("Change"))
            turnover = _to_int(row.get("TradeValue"))
            if close is None or change is None:
                continue
            prev = close - change
            pct = (change / prev * 100.0) if prev > 0 else 0.0
            out.append(
                StockQuote(
                    stock_id=sid,
                    name=str(row.get("Name") or "").strip(),
                    close=close,
                    change=change,
                    pct=pct,
                    turnover=turnover or 0,
                    market="TWSE",
                    shares_outstanding=int(shares_map.get(sid, 0) or 0),
                )
            )
        logger.info("TWSE STOCK_DAY_ALL: %d 筆 → 過濾後 %d 檔普通股", len(rows), len(out))
        return out

    def fetch_tpex_quotes(self) -> List[StockQuote]:
        """TPEX daily_close_quotes → 上櫃個股 list。

        ``Capitals`` 欄即實收資本額；面額 10 元，已發行股數 = Capitals / 10 → 推算市值。
        ``Change`` 欄需 robust parse（可能含「除息」「除權」等文字）。
        """
        rows = self._http.get_json(TPEX_DAILY_CLOSE_QUOTES_URL, params=None)
        out: List[StockQuote] = []
        if not isinstance(rows, list):
            return out
        for row in rows:
            sid = str(row.get("SecuritiesCompanyCode") or "").strip()
            if not _is_common_stock_id(sid):
                continue
            close = _to_float(row.get("Close"))
            change = _parse_change_field(row.get("Change"))
            turnover = _to_int(row.get("TransactionAmount"))
            if close is None or change is None:
                continue
            prev = close - change
            pct = (change / prev * 100.0) if prev > 0 else 0.0
            capital = _to_int(row.get("Capitals")) or 0
            shares = capital // 10 if capital else 0
            out.append(
                StockQuote(
                    stock_id=sid,
                    name=str(row.get("CompanyName") or "").strip(),
                    close=close,
                    change=change,
                    pct=pct,
                    turnover=turnover or 0,
                    market="TPEX",
                    shares_outstanding=shares,
                )
            )
        logger.info("TPEX daily_close_quotes: %d 筆 → 過濾後 %d 檔普通股", len(rows), len(out))
        return out

    def fetch_mi_index(self) -> List[dict]:
        """TWSE MI_INDEX → 全部指數當日收盤行情列。"""
        rows = self._http.get_json(TWSE_MI_INDEX_URL, params=None)
        return rows if isinstance(rows, list) else []


# ---- 聚合 -------------------------------------------------------------------


def build_index_summary(
    *,
    mi_index_rows: List[dict],
    twse_quotes: List[StockQuote],
    tpex_quotes: List[StockQuote],
    meta_map: Dict[str, StockMeta],
    tpex_index_pair: Optional[Tuple[float, float]] = None,
) -> List[IndexQuote]:
    """組裝 4 大指數：加權、櫃買、電子、金融。

    指數收盤／漲跌點數／%：
      - 加權／電子／金融：從 MI_INDEX 抓
      - 櫃買：MI_INDEX 不含 OTC；若 caller 提供 ``tpex_index_pair=(prev_close, today_close)``，
              用 FinMind ``TaiwanStockPrice`` data_id=TPEx 取兩日 K 線推算收盤＋漲跌＋%。
              無資料時 close/diff = 0，pct 退回成份股平均。

    漲跌家數：
      - 加權：TWSE quotes 全集
      - 電子：TWSE quotes 中 industry ∈ ELECTRONIC_INDUSTRIES
      - 金融：TWSE quotes 中 industry ∈ FINANCIAL_INDUSTRIES
      - 櫃買：TPEX quotes 全集
    """
    by_idx_name: Dict[str, dict] = {}
    for r in mi_index_rows:
        name = str(r.get(_MI_KEYS["name"]) or "").strip()
        if name:
            by_idx_name[name] = r

    def _index_from_mi(name: str) -> Tuple[float, float, float]:
        r = by_idx_name.get(name)
        if not r:
            return 0.0, 0.0, 0.0
        close = _to_float(r.get(_MI_KEYS["close"])) or 0.0
        pts = _to_float(_strip_commas(r.get(_MI_KEYS["pts"]))) or 0.0
        sign = str(r.get(_MI_KEYS["sign"]) or "").strip()
        if sign == "-":
            pts = -abs(pts)
        elif sign == "+":
            pts = abs(pts)
        pct = _to_float(r.get(_MI_KEYS["pct"])) or 0.0
        if sign == "-" and pct > 0:
            pct = -pct
        return close, pts, pct

    def _breadth(quotes: Iterable[StockQuote]) -> Tuple[int, int, int]:
        a = d = u = 0
        for q in quotes:
            if q.change > 0:
                a += 1
            elif q.change < 0:
                d += 1
            else:
                u += 1
        return a, d, u

    def _filter_by_industry(quotes: List[StockQuote], industries: frozenset[str]) -> List[StockQuote]:
        out: List[StockQuote] = []
        for q in quotes:
            meta = meta_map.get(q.stock_id)
            if meta and meta.industry in industries:
                out.append(q)
        return out

    taiex_close, taiex_diff, taiex_pct = _index_from_mi(TAIEX_NAME)
    elec_close, elec_diff, elec_pct = _index_from_mi(ELECTRONIC_INDEX_NAME)
    fin_close, fin_diff, fin_pct = _index_from_mi(FINANCIAL_INDEX_NAME)

    # 櫃買：優先用 FinMind TPEx K 線（caller 注入），無則退回成份股平均 pct
    tpex_a, tpex_d, tpex_u = _breadth(tpex_quotes)
    if tpex_index_pair and tpex_index_pair[0] > 0 and tpex_index_pair[1] > 0:
        prev_c, today_c = tpex_index_pair
        tpex_close = today_c
        tpex_diff = today_c - prev_c
        tpex_pct = (tpex_diff / prev_c * 100.0) if prev_c > 0 else 0.0
    else:
        tpex_close = 0.0
        tpex_diff = 0.0
        tpex_pct = (
            sum(q.pct for q in tpex_quotes) / len(tpex_quotes) if tpex_quotes else 0.0
        )

    twse_a, twse_d, twse_u = _breadth(twse_quotes)
    elec_quotes = _filter_by_industry(twse_quotes, ELECTRONIC_INDUSTRIES)
    elec_a, elec_d, elec_u = _breadth(elec_quotes)
    fin_quotes = _filter_by_industry(twse_quotes, FINANCIAL_INDUSTRIES)
    fin_a, fin_d, fin_u = _breadth(fin_quotes)

    return [
        IndexQuote("加權指數", taiex_close, taiex_diff, taiex_pct, twse_a, twse_d, twse_u),
        IndexQuote("櫃買指數", tpex_close, tpex_diff, tpex_pct, tpex_a, tpex_d, tpex_u),
        IndexQuote("電子指數", elec_close, elec_diff, elec_pct, elec_a, elec_d, elec_u),
        IndexQuote("金融指數", fin_close, fin_diff, fin_pct, fin_a, fin_d, fin_u),
    ]


# TPEx 上櫃 industry 命名常與 TWSE 不同（多「業」字尾或異名），這裡 normalize 為
# TWSE 命名，避免 treemap 同一族群被切兩塊。
INDUSTRY_ALIASES: Dict[str, str] = {
    "創新版股票業": "創新版股票",
    "綠能環保業": "綠能環保",
    "數位雲端業": "數位雲端",
    "農業科技業": "農業科技",
    "居家生活業": "居家生活",
    "觀光餐旅": "觀光事業",
    "通訊網路業": "通信網路業",
}


def _normalize_industry(name: str) -> str:
    """合併 TWSE / TPEx 同性質產業命名。"""
    return INDUSTRY_ALIASES.get(name, name)


def build_industry_flow(
    *,
    twse_quotes: List[StockQuote],
    tpex_quotes: List[StockQuote],
    meta_map: Dict[str, StockMeta],
    top_n: Optional[int] = 10,
) -> Tuple[List[IndustryFlow], List[IndustryFlow]]:
    """產業聚合 → (淨流入清單, 淨流出清單)。

    判定：``avg_pct >= 0`` 為淨流入、``< 0`` 為淨流出。
    各自依 **產業總市值由大到小排序**（圖示時 treemap 的方塊大小用市值），
    預設 ``top_n=10``（各取市值前 10 大產業），給 ``None`` 則回傳全部。

    回傳的 :class:`IndustryFlow` 同時帶 ``turnover``（當日成交金額）與 ``market_cap``（產業總市值），
    treemap 排版用 market_cap、標籤可選 turnover 或 market_cap。

    TWSE / TPEx 命名差異（如「觀光餐旅」vs「觀光事業」、TPEx 多出「業」字尾）
    會透過 :data:`INDUSTRY_ALIASES` 合併。
    """
    by_ind: Dict[str, List[StockQuote]] = defaultdict(list)
    for q in list(twse_quotes) + list(tpex_quotes):
        meta = meta_map.get(q.stock_id)
        if not meta or not meta.industry or meta.industry == "其他":
            continue
        by_ind[_normalize_industry(meta.industry)].append(q)

    flows: List[IndustryFlow] = []
    for ind, items in by_ind.items():
        if not items:
            continue
        turnover = sum(q.turnover for q in items)
        market_cap = sum(q.market_cap for q in items)
        avg = sum(q.pct for q in items) / len(items)
        flows.append(
            IndustryFlow(
                industry=ind,
                turnover=turnover,
                market_cap=market_cap,
                avg_pct=avg,
                member_count=len(items),
            )
        )

    inflow = sorted([f for f in flows if f.avg_pct >= 0], key=lambda f: -f.market_cap)
    outflow = sorted([f for f in flows if f.avg_pct < 0], key=lambda f: -f.market_cap)
    if top_n is not None:
        inflow = inflow[:top_n]
        outflow = outflow[:top_n]
    return inflow, outflow


# ---- 解析輔助 ---------------------------------------------------------------

_COMMON_STOCK_RE = re.compile(r"^[1-9]\d{3}$")


def _is_common_stock_id(sid: str) -> bool:
    """4 碼純數字、首位非 0（排除 ETF/ETN/權證）。

    例：2330（台積電）合格；00400A、006201 不合格（ETF）；3X12345 不合格（權證）。
    """
    return bool(_COMMON_STOCK_RE.match(sid))


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    s = _strip_commas(str(v)).strip()
    if not s or s in {"--", "-", "—"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(v) -> Optional[int]:
    f = _to_float(v)
    if f is None:
        return None
    return int(f)


def _strip_commas(v) -> str:
    if v is None:
        return ""
    return str(v).replace(",", "").replace(" ", "")


_CHANGE_TEXT_RE = re.compile(r"^([+\-]?)([0-9.]+)$")


def _parse_change_field(raw) -> Optional[float]:
    """TPEX 的 ``Change`` 欄可能含「除息」「除權」「+1.68」「-0.50」等。

    僅在能解析出純數字時回傳，「除息／除權」一概視為 None（除權息日該檔不入家數）。
    """
    if raw is None:
        return None
    s = _strip_commas(str(raw)).strip()
    if not s or "除" in s or s in {"--", "—"}:
        return None
    m = _CHANGE_TEXT_RE.match(s)
    if not m:
        try:
            return float(s)
        except ValueError:
            return None
    sign, num = m.groups()
    try:
        val = float(num)
    except ValueError:
        return None
    return -val if sign == "-" else val
