"""MOPS 重大訊息 fetcher（上市 TWSE / 上櫃 TPEX 開放資料）。

來源：
  - 上市：``TWSE_MATERIAL_NEWS_URL`` (t187ap04_L) 欄位用中文鍵
    （'公司代號', '公司名稱', '主旨 ', '發言日期', '發言時間', '事實發生日', '說明', '符合條款'）
  - 上櫃：``TPEX_MATERIAL_NEWS_URL`` (mopsfin_t187ap04_O) 欄位 'SecuritiesCompanyCode', 'CompanyName',
    '主旨', '發言日期', '發言時間', '事實發生日', '說明', '符合條款'

兩邊欄位 normalize 成 :class:`MaterialNews`：股票代號、公司名、主旨、發言時刻、條款、市場別。

僅當天會出現新公告；先過濾 watchlist，再依時間排序（新 → 舊）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, List, Optional

from financial_news.core.api_endpoints import (
    TPEX_MATERIAL_NEWS_URL,
    TWSE_MATERIAL_NEWS_URL,
)
from financial_news.core.http import HttpClient
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)


@dataclass(frozen=True)
class MaterialNews:
    """單筆重大訊息（標準化）。"""

    stock_id: str
    stock_name: str
    subject: str  # 主旨
    market: str  # "TWSE" / "TPEX"
    announce_dt: Optional[datetime] = None  # 發言時刻
    fact_date: Optional[date] = None  # 事實發生日
    clause: str = ""  # 符合條款（如「第44款」）

    @property
    def sort_key(self):
        """新公告排前面（announce_dt 大者優先）。"""
        return self.announce_dt or datetime.min


def _parse_roc_date(s: str) -> Optional[date]:
    """民國日期 ``1150525`` → :class:`date(2026, 5, 25)`；不合法回 ``None``。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        if len(s) == 7:  # 1150525
            y = int(s[:3]) + 1911
            m = int(s[3:5])
            d = int(s[5:7])
            return date(y, m, d)
        if len(s) == 6:  # 950525 → 早期 6 碼
            y = int(s[:2]) + 1911
            m = int(s[2:4])
            d = int(s[4:6])
            return date(y, m, d)
    except ValueError:
        return None
    return None


def _parse_announce_dt(date_str: str, time_str: str) -> Optional[datetime]:
    """``1150525`` + ``93015`` (HHMMSS, 可省略左 0) → :class:`datetime`。"""
    d = _parse_roc_date(date_str)
    if not d:
        return None
    t = (time_str or "").strip().zfill(6)
    try:
        hh = int(t[:2])
        mm = int(t[2:4])
        ss = int(t[4:6])
        return datetime(d.year, d.month, d.day, hh, mm, ss)
    except ValueError:
        return datetime(d.year, d.month, d.day)


def _row_twse_to_news(row: dict) -> Optional[MaterialNews]:
    sid = (row.get("公司代號") or "").strip()
    if not sid:
        return None
    name = (row.get("公司名稱") or "").strip()
    # 上市端 key 結尾有空格："主旨 "（注意）
    subject = (row.get("主旨 ") or row.get("主旨") or "").strip()
    announce_dt = _parse_announce_dt(row.get("發言日期", ""), row.get("發言時間", ""))
    fact_d = _parse_roc_date(row.get("事實發生日", ""))
    clause = (row.get("符合條款") or "").strip()
    return MaterialNews(
        stock_id=sid,
        stock_name=name,
        subject=subject,
        market="TWSE",
        announce_dt=announce_dt,
        fact_date=fact_d,
        clause=clause,
    )


def _row_tpex_to_news(row: dict) -> Optional[MaterialNews]:
    sid = (row.get("SecuritiesCompanyCode") or "").strip()
    if not sid:
        return None
    name = (row.get("CompanyName") or "").strip()
    subject = (row.get("主旨") or "").strip()
    announce_dt = _parse_announce_dt(row.get("發言日期", ""), row.get("發言時間", ""))
    fact_d = _parse_roc_date(row.get("事實發生日", ""))
    clause = (row.get("符合條款") or "").strip()
    return MaterialNews(
        stock_id=sid,
        stock_name=name,
        subject=subject,
        market="TPEX",
        announce_dt=announce_dt,
        fact_date=fact_d,
        clause=clause,
    )


def fetch_material_news(*, http: Optional[HttpClient] = None) -> List[MaterialNews]:
    """抓 TWSE + TPEX 重大訊息，合併、去重並依公告時間排序（新 → 舊）。

    任一端點失敗都記 warning 並回該端點空清單，不影響另一邊。

    去重規則：以 ``(stock_id, normalized_subject, announce_dt)`` 為 key；
    上游偶有同主旨同時刻重複登錄的情況（含上市/上櫃資料源交叉），
    若不去重會在「重大訊息」段出現一模一樣的兩行，被切到不同則訊息時更明顯。
    主旨會做 trim + 連續空白壓平再比對，避免空白差異被當成不同。
    """
    client = http or HttpClient(timeout=30.0, name="mops_news")
    raw: List[MaterialNews] = []

    try:
        rows = client.get_json(TWSE_MATERIAL_NEWS_URL, tries=2)
        for r in rows or []:
            n = _row_twse_to_news(r)
            if n:
                raw.append(n)
        logger.info("MOPS TWSE 重大訊息：%d 筆", len([x for x in raw if x.market == "TWSE"]))
    except Exception as e:  # noqa: BLE001
        logger.warning("MOPS TWSE 重大訊息抓取失敗：%s", e)

    try:
        rows = client.get_json(TPEX_MATERIAL_NEWS_URL, tries=2)
        tpex_count = 0
        for r in rows or []:
            n = _row_tpex_to_news(r)
            if n:
                raw.append(n)
                tpex_count += 1
        logger.info("MOPS TPEX 重大訊息：%d 筆", tpex_count)
    except Exception as e:  # noqa: BLE001
        logger.warning("MOPS TPEX 重大訊息抓取失敗：%s", e)

    # 去重：(sid, normalized_subject, announce_dt)
    seen: set = set()
    out: List[MaterialNews] = []
    dup_count = 0
    for n in raw:
        subj_norm = " ".join((n.subject or "").split())
        key = (n.stock_id, subj_norm, n.announce_dt)
        if key in seen:
            dup_count += 1
            continue
        seen.add(key)
        out.append(n)
    if dup_count:
        logger.info("MOPS 去重移除 %d 筆同主旨同時刻重複", dup_count)

    out.sort(key=lambda x: x.sort_key, reverse=True)
    return out


def format_material_news_block(
    items: List[MaterialNews],
    *,
    watch_tickers: Iterable[str] = (),
    max_total: int = 30,
    max_watch: int = 20,
    max_other: int = 10,
) -> str:
    """把重大訊息整理成文字段：watchlist 個股優先、其餘截短。

    LINE 單則訊息 5000 字上限、單則文字訊息實務上 ~3500 字會被切，所以採：
      - watchlist 個股最多 ``max_watch`` 筆（保證顯示）
      - 其他個股最多 ``max_other`` 筆
      - 總筆數不超過 ``max_total``
    每筆顯示「股號 公司｜主旨」（主旨太長截到 40 字）。
    """
    if not items:
        return ""

    watch = set(watch_tickers)
    watch_items: List[MaterialNews] = []
    other_items: List[MaterialNews] = []
    for it in items:
        if it.stock_id in watch:
            watch_items.append(it)
        else:
            other_items.append(it)

    watch_items = watch_items[:max_watch]
    remaining = max(0, max_total - len(watch_items))
    other_items = other_items[: min(max_other, remaining)]

    if not watch_items and not other_items:
        return ""

    lines = ["【重大訊息（公司公告，watchlist 優先）】", ""]

    def _fmt(n: MaterialNews) -> str:
        subj = n.subject or "—"
        if len(subj) > 40:
            subj = subj[:38] + "…"
        time_part = ""
        if n.announce_dt:
            time_part = f" {n.announce_dt.strftime('%m/%d %H:%M')}"
        label = f"{n.stock_id} {n.stock_name}".strip()
        return f"・{label}{time_part}｜{subj}"

    if watch_items:
        lines.append(f"— 觀察清單（{len(watch_items)} 檔）")
        lines.extend(_fmt(n) for n in watch_items)
        lines.append("")
    if other_items:
        lines.append(f"— 其他（{len(other_items)} 檔）")
        lines.extend(_fmt(n) for n in other_items)

    return "\n".join(lines).rstrip()
