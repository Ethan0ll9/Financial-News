"""事件「未來預告（lookahead）」與「進行中（in-progress）」彙整。

核心精神：盤前簡報每天滾動更新「今日 + 未來 N 個交易日」與「現正在進行的期間
事件」，取代僅在週一列出「本週」的舊設計，避免遺漏。

涵蓋：
- 預告：除權息、股東會、融券強制回補、停牌復牌日（依 settings.kinds 控制）
- 進行中：處置股期間、停止過戶期間
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

from financial_news.tw_briefing.date_parsing import parse_iso_date
from financial_news.tw_briefing.exdividend import (
    ExDividendEvent,
    events_in_date_range,
)
from financial_news.tw_briefing.official_events import SuspendedEvent
from financial_news.tw_briefing.tw_calendar import TwCalendar
from financial_news.tw_briefing.twse_announcements import (
    DisposalStock,
    ShareholderMeeting,
    _roc_seg_to_date,
)


# ---- dataclasses --------------------------------------------------------------


@dataclass(frozen=True)
class LookaheadItem:
    """單筆「未來預告」的標準化條目。"""

    target_date: date          # 事件落在哪一天（D-Day）
    countdown: int             # 距 base_date 幾天（0=今日，1=D-1，2=D-2…）
    kind: str                  # exdiv / shareholder_meeting / short_cover / suspended_resume
    stock_id: str
    stock_name: str
    note: str = ""


@dataclass(frozen=True)
class InProgressItem:
    """處置期間 / 停止過戶期間「現正進行」條目。"""

    kind: str                  # disposal / book_close
    stock_id: str
    stock_name: str
    period_start: Optional[date]
    period_end: Optional[date]
    detail: str = ""


# ---- 預告（含倒數天）---------------------------------------------------------


_KIND_LABEL = {
    "exdiv": "除權息",
    "shareholder_meeting": "股東會",
    "short_cover": "融券強制回補",
    "suspended_resume": "復牌",
}


def _emoji(kind: str) -> str:
    return {
        "exdiv": "🟢",
        "shareholder_meeting": "📋",
        "short_cover": "🔁",
        "suspended_resume": "▶️",
    }.get(kind, "・")


def _resumption_date_of(s: SuspendedEvent) -> Optional[date]:
    return parse_iso_date(s.resumption_date)


def _suspended_start_of(s: SuspendedEvent) -> Optional[date]:
    return parse_iso_date(s.announce_date)


def collect_lookahead(
    *,
    base_date: date,
    cal: TwCalendar,
    n_trading_days: int,
    include_kinds: Iterable[str],
    tw48_rows: Optional[List[dict]] = None,
    sh_meetings: Optional[List[ShareholderMeeting]] = None,
    suspended: Optional[List[SuspendedEvent]] = None,
    watch_tickers: Optional[Iterable[str]] = None,
) -> List[LookaheadItem]:
    """彙整 base_date 之後的 n 個交易日內的事件。

    ``include_kinds``：可包含 ``exdiv`` / ``shareholder_meeting`` / ``short_cover``
    / ``suspended_resume``。今日（D-Day=0）通常已有獨立區塊，此函式預設「不含
    今日」，僅列 D-1 ~ D-N（如需含今日，呼叫端自行加入；今日資料已由
    ``_format_today_events`` 顯示）。

    ``watch_tickers``：若提供，``shareholder_meeting`` / ``short_cover`` 只會保留
    該 set 內的個股（避免上千家公司全部塞進預告）。除權息與停復牌不受影響。
    """
    target_days = cal.next_n_trading_days(base_date, n_trading_days)
    if not target_days:
        return []
    target_set = set(target_days)
    kinds = set(include_kinds)
    watch_set: Optional[set] = set(watch_tickers) if watch_tickers is not None else None
    out: List[LookaheadItem] = []

    if "exdiv" in kinds and tw48_rows is not None:
        first, last = target_days[0], target_days[-1]
        for e in events_in_date_range(tw48_rows, first, last):
            if e.ex_date in target_set:
                out.append(_from_exdiv(e, base_date))

    if sh_meetings:
        if "shareholder_meeting" in kinds:
            for m in sh_meetings:
                if m.meeting_date and m.meeting_date in target_set:
                    if watch_set is not None and m.stock_id not in watch_set:
                        continue
                    out.append(_from_meeting(m, base_date))
        if "short_cover" in kinds:
            for m in sh_meetings:
                if m.short_cover_due and m.short_cover_due in target_set:
                    if watch_set is not None and m.stock_id not in watch_set:
                        continue
                    out.append(_from_short_cover(m, base_date))

    if "suspended_resume" in kinds and suspended:
        for s in suspended:
            r = _resumption_date_of(s)
            if r and r in target_set:
                out.append(_from_resume(s, r, base_date))

    out.sort(key=lambda x: (x.target_date, x.kind, x.stock_id))
    return out


def _from_exdiv(e: ExDividendEvent, base: date) -> LookaheadItem:
    return LookaheadItem(
        target_date=e.ex_date,
        countdown=(e.ex_date - base).days,
        kind="exdiv",
        stock_id=e.stock_id,
        stock_name=e.stock_name,
        note=e.note,
    )


def _from_meeting(m: ShareholderMeeting, base: date) -> LookaheadItem:
    return LookaheadItem(
        target_date=m.meeting_date,  # type: ignore[arg-type]
        countdown=(m.meeting_date - base).days,  # type: ignore[operator]
        kind="shareholder_meeting",
        stock_id=m.stock_id,
        stock_name=m.stock_name,
        note=m.meeting_kind or "常會",
    )


def _from_short_cover(m: ShareholderMeeting, base: date) -> LookaheadItem:
    detail = ""
    if m.book_close_start:
        detail = f"停過戶起 {m.book_close_start.isoformat()}"
    return LookaheadItem(
        target_date=m.short_cover_due,  # type: ignore[arg-type]
        countdown=(m.short_cover_due - base).days,  # type: ignore[operator]
        kind="short_cover",
        stock_id=m.stock_id,
        stock_name=m.stock_name,
        note=detail,
    )


def _from_resume(s: SuspendedEvent, resume_d: date, base: date) -> LookaheadItem:
    return LookaheadItem(
        target_date=resume_d,
        countdown=(resume_d - base).days,
        kind="suspended_resume",
        stock_id=s.stock_id,
        stock_name="",
        note="復牌",
    )


def _countdown_label(cd: int) -> str:
    if cd <= 0:
        return "D-Day"
    return f"D-{cd}"


def format_lookahead_block(
    items: List[LookaheadItem],
    *,
    base_date: date,
    max_per_kind_per_day: int = 5,
) -> str:
    """以「日期分組」格式化未來預告區塊。

    ``max_per_kind_per_day``：同一天同一種事件（如股東會）最多顯示幾筆，
    超過以「及其他 N 家」摘要替代，避免大量股東會撐爆訊息長度。
    """
    lines: List[str] = ["【近 5 日事件預告】", ""]
    if not items:
        lines.append("（未來 5 個交易日內無已知重點事件）")
        return "\n".join(lines)

    by_day: Dict[date, List[LookaheadItem]] = defaultdict(list)
    for it in items:
        by_day[it.target_date].append(it)
    for d in sorted(by_day.keys()):
        first = by_day[d][0]
        cd = _countdown_label(first.countdown)
        lines.append(f"— {d.isoformat()}（{cd}）")
        # 按 kind 分組，各 kind 分別限制顯示筆數
        by_kind: Dict[str, List[LookaheadItem]] = defaultdict(list)
        for it in by_day[d]:
            by_kind[it.kind].append(it)
        for kind in sorted(by_kind.keys()):
            kind_items = by_kind[kind]
            shown = kind_items[:max_per_kind_per_day]
            overflow = len(kind_items) - len(shown)
            for it in shown:
                label = _KIND_LABEL.get(it.kind, it.kind)
                emoji = _emoji(it.kind)
                name = f" {it.stock_name}" if it.stock_name else ""
                note = f"｜{it.note}" if it.note else ""
                lines.append(f"・{emoji} {label}｜{it.stock_id}{name}{note}")
            if overflow > 0:
                label = _KIND_LABEL.get(kind, kind)
                emoji = _emoji(kind)
                lines.append(f"・{emoji} {label}｜及其他 {overflow} 家")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---- 進行中 -----------------------------------------------------------------


def _disposal_period(d: DisposalStock) -> Tuple[Optional[date], Optional[date]]:
    seg = (d.period or "").replace("～", "~").split("~")
    if len(seg) == 2:
        return _roc_seg_to_date(seg[0].strip()), _roc_seg_to_date(seg[1].strip())
    return None, None


def collect_in_progress(
    *,
    today: date,
    disposals: Optional[List[DisposalStock]] = None,
    sh_meetings: Optional[List[ShareholderMeeting]] = None,
    include_disposal: bool = True,
    include_book_close: bool = True,
    watch_tickers: Optional[Iterable[str]] = None,
) -> List[InProgressItem]:
    """彙整今日仍在「進行期間」內的事件。

    ``watch_tickers``：若提供，``book_close``（停止過戶期間）只會保留該 set 內的
    個股；``disposal`` 不受影響（家數本來就少，全列即可）。
    """
    watch_set: Optional[set] = set(watch_tickers) if watch_tickers is not None else None
    out: List[InProgressItem] = []
    if include_disposal and disposals:
        for d in disposals:
            ps, pe = _disposal_period(d)
            if ps and pe and ps <= today <= pe:
                out.append(
                    InProgressItem(
                        kind="disposal",
                        stock_id=d.stock_id,
                        stock_name=d.stock_name,
                        period_start=ps,
                        period_end=pe,
                        detail=f"{d.measure}｜{d.reason}",
                    )
                )
    if include_book_close and sh_meetings:
        for m in sh_meetings:
            if watch_set is not None and m.stock_id not in watch_set:
                continue
            if m.book_close_start and m.book_close_end and (
                m.book_close_start <= today <= m.book_close_end
            ):
                out.append(
                    InProgressItem(
                        kind="book_close",
                        stock_id=m.stock_id,
                        stock_name=m.stock_name,
                        period_start=m.book_close_start,
                        period_end=m.book_close_end,
                        detail=(
                            f"{m.meeting_kind} 股東會 "
                            f"{m.meeting_date.isoformat() if m.meeting_date else '—'}"
                        ),
                    )
                )
    out.sort(key=lambda x: (x.kind, x.stock_id))
    return out


def format_in_progress_block(
    items: List[InProgressItem],
    *,
    today: date,
    max_per_kind: int = 10,
) -> str:
    """格式化進行中事件。

    ``max_per_kind``：每種事件最多明細幾筆（預設 10），超過以「及其他 N 家」代替。
    stock_close 通常達數百筆，預設值可有效壓縮訊息長度。
    """
    lines: List[str] = ["【進行中事件（期間覆蓋今日）】", ""]
    if not items:
        lines.append("（今日無進行中之處置／停止過戶期間）")
        return "\n".join(lines)

    grouped: Dict[str, List[InProgressItem]] = defaultdict(list)
    for it in items:
        grouped[it.kind].append(it)

    section_titles = {
        "disposal": "🚫 處置股（處置期間中）",
        "book_close": "🧾 停止過戶（股東會前）",
    }
    for k in ("disposal", "book_close"):
        if k not in grouped:
            continue
        all_items = grouped[k]
        shown = all_items[:max_per_kind]
        overflow = len(all_items) - len(shown)
        lines.append(f"— {section_titles[k]}（{len(all_items)} 檔）")
        for it in shown:
            remaining = ""
            if it.period_end:
                days = (it.period_end - today).days
                remaining = f"｜剩 {days} 天"
            period = ""
            if it.period_start and it.period_end:
                period = f"{it.period_start.isoformat()} ~ {it.period_end.isoformat()}"
            lines.append(
                f"・{it.stock_id} {it.stock_name}｜{it.detail}｜{period}{remaining}"
            )
        if overflow > 0:
            lines.append(f"・（及其他 {overflow} 家，完整清單見 HTML 報告）")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---- 盤後「明日預告」精簡版 --------------------------------------------------


def format_next_day_brief(
    items: List[LookaheadItem],
    *,
    next_trading_day: Optional[date],
) -> str:
    """從 lookahead items 萃取「next_trading_day（D-1）」當天事件，做一行精簡列。"""
    if not next_trading_day:
        return ""
    one_day = [x for x in items if x.target_date == next_trading_day]
    if not one_day:
        return f"【明日預告（{next_trading_day.isoformat()}）】（無已知事件）"
    counts: Dict[str, int] = defaultdict(int)
    samples: Dict[str, List[str]] = defaultdict(list)
    for it in one_day:
        counts[it.kind] += 1
        if len(samples[it.kind]) < 3:
            nm = f" {it.stock_name}" if it.stock_name else ""
            samples[it.kind].append(f"{it.stock_id}{nm}")
    parts: List[str] = []
    for k, n in counts.items():
        label = _KIND_LABEL.get(k, k)
        emoji = _emoji(k)
        sample = "、".join(samples[k])
        more = f"…+{n - 3}" if n > 3 else ""
        parts.append(f"{emoji} {label} {n} 檔（{sample}{more}）")
    return f"【明日預告（{next_trading_day.isoformat()}）】" + "｜".join(parts)
