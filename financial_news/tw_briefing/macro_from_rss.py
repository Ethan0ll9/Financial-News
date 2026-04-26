"""隔夜海外：由 digest RSS 標題規則分組（無 LLM）。"""
from __future__ import annotations

import re
from typing import Iterable, List, Set, Tuple

from financial_news.models import NewsItem

_TW_REGION_HINTS = frozenset(
    {"台灣", "tw", "taiwan", "臺灣", "櫃買", "證交所", "twse", "tpex"}
)


def _is_twish(item: NewsItem) -> bool:
    r = (item.region or "").strip().lower()
    if r in _TW_REGION_HINTS:
        return True
    host = ""
    if item.url:
        from urllib.parse import urlparse

        host = (urlparse(item.url).netloc or "").lower()
    if host.endswith(".tw"):
        return True
    return False


def _norm(s: str) -> str:
    return s.strip().lower()


def _bucket(title: str) -> str | None:
    t = _norm(title)
    if not t:
        return None

    rules: List[Tuple[str, Tuple[str, ...]]] = [
        (
            "商品",
            (
                "原油",
                "金價",
                "黃金",
                "銅價",
                "商品",
                "oil",
                "opec",
                "gold",
                "silver",
                "wti",
                "brent",
            ),
        ),
        (
            "債券／利率",
            (
                "債券",
                "公債",
                "殖利率",
                "美債",
                "國債",
                "treasury",
                "yield",
                "降息",
                "升息",
                "利率",
            ),
        ),
        (
            "匯率",
            (
                "美元",
                "日圓",
                "歐元",
                "英鎊",
                "匯率",
                "forex",
                "yen",
                "euro",
                "dollar",
                "fx",
            ),
        ),
        (
            "美股／總經",
            (
                "美股",
                "nasdaq",
                "s&p",
                "標普",
                "道瓊",
                "dow",
                "fed",
                "cpi",
                "pce",
                "非農",
                "美國",
                "矽谷",
                "華爾街",
                "mag 7",
                "magnificent",
                "nvidia",
                "輝達",
                "apple",
                "特斯拉",
                "tesla",
                "財報季",
            ),
        ),
    ]
    for label, kws in rules:
        for kw in kws:
            if kw in t:
                return label
    return None


def _dedupe_lines(lines: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for ln in lines:
        key = re.sub(r"\s+", " ", ln.strip())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(ln.strip())
    return out


def format_macro_from_rss(items: Iterable[NewsItem], *, max_per_bucket: int = 5) -> str:
    """產出盤前「隔夜海外」文字區塊。"""
    buckets: dict[str, List[str]] = {
        "美股／總經": [],
        "債券／利率": [],
        "匯率": [],
        "商品": [],
    }
    for it in items:
        if _is_twish(it):
            continue
        b = _bucket(it.title)
        if not b or b not in buckets:
            continue
        line = f"・{it.title}"
        if len(buckets[b]) < max_per_bucket:
            buckets[b].append(line)

    lines: List[str] = ["【隔夜海外（RSS 標題節選）】", ""]
    empty = True
    for name in ("美股／總經", "債券／利率", "匯率", "商品"):
        chunk = _dedupe_lines(buckets[name])
        if not chunk:
            continue
        empty = False
        lines.append(f"■ {name}")
        lines.extend(chunk)
        lines.append("")
    if empty:
        lines.append("（本輪國際 RSS 中未匹配到美股／債／匯／商品關鍵字，或 digest 未含國際來源）")
    return "\n".join(lines).rstrip()


def format_tw_event_hints_from_rss(items: Iterable[NewsItem], *, max_items: int = 8) -> str:
    """台股相關敘事線索（非法定公告）：法說、現增、可轉債等關鍵字。"""
    kws = (
        "法說",
        "法說會",
        "線上法說",
        "現金增資",
        "現增",
        "可轉債",
        "轉換公司債",
        "停券",
        "股東會",
        "減資",
        "分點",
        "處置",
        "注意股票",
        "指數調整",
        "成分股",
    )
    lines: List[str] = []
    for it in items:
        t = it.title.strip()
        tl = t.lower()
        if not any(k.lower() in tl or k in t for k in kws):
            continue
        if not _is_twish(it):
            # 仍允許未標 region 但標題明顯台股代號 4 碼
            if not re.search(r"\b\d{4}\b", t):
                continue
        lines.append(f"・{t}\n  {it.url}")
        if len(lines) >= max_items:
            break
    if not lines:
        return "（RSS 無明顯法說／現增／可轉債等關鍵字標題）"
    return "【台股敘事線索（RSS 關鍵字）】\n" + "（非官方公告日曆，僅供參考）\n\n" + "\n\n".join(lines)
