"""TWSE OpenAPI 各類公告：注意股、處置股、股東會（含融券強制回補日推算）。

來源（2026 年實測仍可用）：
- 注意股: https://openapi.twse.com.tw/v1/announcement/notice
- 處置股: https://openapi.twse.com.tw/v1/announcement/punish
- 股東會: https://openapi.twse.com.tw/v1/opendata/t187ap38_L

註解：
- 「股東會前融券強制回補」依規定為**停止過戶日的前 6 個營業日**回補；
  本模組僅以「停止過戶起始日 - 6 工作日」當粗略推算（若需精確，可改以
  TwCalendar 真實交易日往前算 6 天，呼叫端可自行包裝）。
- 法說會與「即時重大訊息」TWSE OpenAPI 該 endpoint 已停用（500），本模組暫不提供；
  若未來改走 MOPS，請另開 fetcher。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

from financial_news.tw_briefing.exdividend import roc_minguo_date_to_gregorian
from financial_news.utils import setup_logger

logger = setup_logger(__name__)

NOTICE_URL = "https://openapi.twse.com.tw/v1/announcement/notice"
PUNISH_URL = "https://openapi.twse.com.tw/v1/announcement/punish"
SHAREHOLDER_MEETING_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap38_L"


# ---- dataclasses --------------------------------------------------------------


@dataclass(frozen=True)
class AttentionStock:
    """注意股票公告（連續異常波動等）。"""

    announce_date: date
    stock_id: str
    stock_name: str
    closing_price: float
    pe_ratio: float
    note: str  # TradingInfoForAttention（公告原因標籤）


@dataclass(frozen=True)
class DisposalStock:
    """處置股票公告（連續注意 → 處置）。"""

    announce_date: date
    stock_id: str
    stock_name: str
    reason: str          # ReasonsOfDisposition
    period: str          # DispositionPeriod e.g. 115/04/27～115/05/11
    measure: str         # DispositionMeasures e.g. 第一次處置


@dataclass(frozen=True)
class ShareholderMeeting:
    """股東常／臨時會 + 推算的融券強制回補日。"""

    issue_date: date            # 出表日期
    stock_id: str
    stock_name: str
    meeting_kind: str           # 常會 / 臨時會
    meeting_date: Optional[date]
    book_close_start: Optional[date]
    book_close_end: Optional[date]
    short_cover_due: Optional[date]   # 推估：停止過戶起始日 - 6 個曆日（粗略）
    note: str = ""


# ---- helpers -----------------------------------------------------------------


def _safe_float(s: Any) -> float:
    try:
        return float(str(s).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _http_get_json(url: str) -> List[Dict[str, Any]]:
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except (requests.RequestException, ValueError) as e:
        logger.warning("TWSE OpenAPI 擷取失敗 %s: %s", url, e)
        return []


def _row_is_blank(row: Dict[str, Any]) -> bool:
    code = str(row.get("Code") or row.get("公司代號") or "").strip()
    return not code or code in {"0", ""}


# ---- 注意股 -------------------------------------------------------------------


def parse_attention_row(row: Dict[str, Any]) -> Optional[AttentionStock]:
    if _row_is_blank(row):
        return None
    d = roc_minguo_date_to_gregorian(str(row.get("Date") or ""))
    if d is None:
        return None
    return AttentionStock(
        announce_date=d,
        stock_id=str(row.get("Code") or "").strip(),
        stock_name=str(row.get("Name") or "").strip(),
        closing_price=_safe_float(row.get("ClosingPrice")),
        pe_ratio=_safe_float(row.get("PE")),
        note=str(row.get("TradingInfoForAttention") or "").strip(),
    )


def fetch_attention_stocks() -> List[AttentionStock]:
    rows = _http_get_json(NOTICE_URL)
    out = [parse_attention_row(r) for r in rows]
    out = [x for x in out if x is not None]
    out.sort(key=lambda x: (x.announce_date, x.stock_id))
    return out


# ---- 處置股 -------------------------------------------------------------------


def parse_disposal_row(row: Dict[str, Any]) -> Optional[DisposalStock]:
    if _row_is_blank(row):
        return None
    d = roc_minguo_date_to_gregorian(str(row.get("Date") or ""))
    if d is None:
        return None
    return DisposalStock(
        announce_date=d,
        stock_id=str(row.get("Code") or "").strip(),
        stock_name=str(row.get("Name") or "").strip(),
        reason=str(row.get("ReasonsOfDisposition") or "").strip(),
        period=str(row.get("DispositionPeriod") or "").strip(),
        measure=str(row.get("DispositionMeasures") or "").strip(),
    )


def fetch_disposal_stocks() -> List[DisposalStock]:
    rows = _http_get_json(PUNISH_URL)
    out = [parse_disposal_row(r) for r in rows]
    out = [x for x in out if x is not None]
    out.sort(key=lambda x: (x.announce_date, x.stock_id))
    return out


# ---- 股東會 + 融券強制回補日 ---------------------------------------------------


def _roc_or_none(s: Any) -> Optional[date]:
    raw = str(s or "").strip()
    if not raw:
        return None
    return roc_minguo_date_to_gregorian(raw)


def parse_shareholder_meeting_row(row: Dict[str, Any]) -> Optional[ShareholderMeeting]:
    code = str(row.get("公司代號") or "").strip()
    if not code:
        return None
    issue = _roc_or_none(row.get("出表日期"))
    if issue is None:
        return None
    meeting_d = _roc_or_none(row.get("股東常(臨時)會日期-日期"))
    bc_start = _roc_or_none(row.get("停止過戶起訖日期-起"))
    bc_end = _roc_or_none(row.get("停止過戶起訖日期-訖"))
    # 融券強制回補日：停止過戶起始日的前 6 個曆日（粗略；TwCalendar 可進階修正）
    short_cover = bc_start - timedelta(days=6) if bc_start else None
    return ShareholderMeeting(
        issue_date=issue,
        stock_id=code,
        stock_name=str(row.get("公司名稱") or "").strip(),
        meeting_kind=str(row.get("股東常(臨時)會日期-常或臨時") or "常會").strip(),
        meeting_date=meeting_d,
        book_close_start=bc_start,
        book_close_end=bc_end,
        short_cover_due=short_cover,
        note=str(row.get("種類") or "").strip(),
    )


def fetch_shareholder_meetings() -> List[ShareholderMeeting]:
    rows = _http_get_json(SHAREHOLDER_MEETING_URL)
    out = [parse_shareholder_meeting_row(r) for r in rows]
    out = [x for x in out if x is not None]
    # 用「股東會日期」排序，沒填者排最後
    out.sort(key=lambda x: (x.meeting_date or date(9999, 12, 31), x.stock_id))
    return out


# ---- 篩選工具 ----------------------------------------------------------------


def attentions_on(items: List[AttentionStock], target: date) -> List[AttentionStock]:
    return [x for x in items if x.announce_date == target]


def disposals_on(items: List[DisposalStock], target: date) -> List[DisposalStock]:
    """處置股：以「公告日 == target」或「處置期間涵蓋 target」當作命中。"""
    out: List[DisposalStock] = []
    iso = target.isoformat()
    for x in items:
        if x.announce_date == target:
            out.append(x)
            continue
        # 處置期間 e.g. "115/04/27～115/05/11"
        try:
            seg = x.period.replace("～", "~").split("~")
            if len(seg) == 2:
                s = _roc_seg_to_date(seg[0].strip())
                e = _roc_seg_to_date(seg[1].strip())
                if s and e and s <= target <= e:
                    out.append(x)
        except (ValueError, AttributeError):
            continue
    return out


def _roc_seg_to_date(s: str) -> Optional[date]:
    """處置期間用『115/04/27』格式（含斜線）；轉西元 date。"""
    try:
        parts = s.split("/")
        if len(parts) != 3:
            return None
        y = int(parts[0]) + 1911
        m = int(parts[1])
        d = int(parts[2])
        return date(y, m, d)
    except (ValueError, TypeError):
        return None


def short_cover_on(items: List[ShareholderMeeting], target: date) -> List[ShareholderMeeting]:
    """融券強制回補日 == target（粗略：停止過戶起始日 -6 曆日）。"""
    return [x for x in items if x.short_cover_due == target]


def shareholder_meetings_on(items: List[ShareholderMeeting], target: date) -> List[ShareholderMeeting]:
    return [x for x in items if x.meeting_date == target]
