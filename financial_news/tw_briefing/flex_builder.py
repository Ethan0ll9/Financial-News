"""LINE Flex Message 構建：單一 Bubble，展示指數卡 + 熱門族群 + 熱門個股。

不依賴 line-bot-sdk，直接組 dict。樣式遵循 LINE Flex 官方規範，
對 Android/iOS/Desktop LINE 都能正常顯示；超長內容會自動截斷避免 payload 過大。
"""
from __future__ import annotations

from typing import List, Optional

from financial_news.tw_briefing.finmind_client import IndexBar
from financial_news.tw_briefing.market_queries import ProxyStat
from financial_news.tw_briefing.theme_detect import MarketDigest, ThemeSummary


_COLOR_UP = "#D9534F"
_COLOR_DOWN = "#2D8F5A"
_COLOR_FLAT = "#868E96"
_COLOR_MUTED = "#6B7280"
_COLOR_TEXT = "#1F2937"
_COLOR_HEADER_BG = "#3559A5"


def _pct_color(p: float) -> str:
    if p > 0.05:
        return _COLOR_UP
    if p < -0.05:
        return _COLOR_DOWN
    return _COLOR_FLAT


def _fmt_pct(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}%"


def _fmt_yi(money: int) -> str:
    return f"{money / 1e8:,.1f} 億"


def _kv(label: str, value: str, value_color: Optional[str] = None) -> dict:
    return {
        "type": "box",
        "layout": "horizontal",
        "contents": [
            {
                "type": "text",
                "text": label,
                "color": _COLOR_MUTED,
                "size": "sm",
                "flex": 2,
            },
            {
                "type": "text",
                "text": value,
                "color": value_color or _COLOR_TEXT,
                "size": "sm",
                "flex": 3,
                "align": "end",
                "weight": "bold",
            },
        ],
    }


def _separator(margin: str = "md") -> dict:
    return {"type": "separator", "margin": margin, "color": "#E5E7EB"}


def _header(title: str, subtitle: str) -> dict:
    return {
        "type": "box",
        "layout": "vertical",
        "backgroundColor": _COLOR_HEADER_BG,
        "paddingAll": "16px",
        "contents": [
            {
                "type": "text",
                "text": title,
                "color": "#FFFFFF",
                "weight": "bold",
                "size": "lg",
                "wrap": True,
            },
            {
                "type": "text",
                "text": subtitle,
                "color": "#DCE4F5",
                "size": "xs",
                "margin": "xs",
                "wrap": True,
            },
        ],
    }


def _index_block(
    index_id: str,
    bar: Optional[IndexBar],
    prev_close: Optional[float],
) -> List[dict]:
    items: List[dict] = []
    if not bar:
        items.append(
            {
                "type": "text",
                "text": f"加權（{index_id}）無 K 線資料（建議 TAIEX）",
                "color": _COLOR_MUTED,
                "size": "sm",
                "wrap": True,
            }
        )
        return items

    pct_text = "—"
    pct_color = _COLOR_FLAT
    if prev_close and prev_close > 0:
        p = (bar.close - prev_close) / prev_close * 100.0
        pct_text = _fmt_pct(p)
        pct_color = _pct_color(p)

    items.append(
        {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {
                    "type": "text",
                    "text": f"加權（{index_id}）",
                    "color": _COLOR_MUTED,
                    "size": "xs",
                    "flex": 3,
                },
                {
                    "type": "text",
                    "text": f"{bar.close:,.2f}",
                    "color": _COLOR_TEXT,
                    "weight": "bold",
                    "size": "xl",
                    "flex": 4,
                    "align": "end",
                },
                {
                    "type": "text",
                    "text": pct_text,
                    "color": pct_color,
                    "weight": "bold",
                    "size": "lg",
                    "flex": 3,
                    "align": "end",
                },
            ],
        }
    )
    items.append(_separator("sm"))
    items.append(_kv("開 / 高 / 低", f"{bar.open:,.0f} / {bar.high:,.0f} / {bar.low:,.0f}"))
    items.append(_kv("成交量", f"{_fmt_yi(bar.trading_money)}元"))
    return items


def _themes_block(title: str, themes: List[ThemeSummary], limit: int = 3) -> List[dict]:
    items: List[dict] = [
        {
            "type": "text",
            "text": title,
            "color": _COLOR_MUTED,
            "size": "xs",
            "weight": "bold",
        }
    ]
    if not themes:
        items.append({"type": "text", "text": "（資料不足）", "color": _COLOR_MUTED, "size": "xs"})
        return items
    for t in themes[:limit]:
        leaders = [s for s in t.leaders(2) if s.name]
        leader_line = "  ".join(f"{s.stock_id} {s.name}" for s in leaders) or ""
        items.append(
            {
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {
                        "type": "text",
                        "text": f"{t.industry}（{t.member_count}）",
                        "color": _COLOR_TEXT,
                        "size": "sm",
                        "flex": 4,
                        "weight": "bold",
                    },
                    {
                        "type": "text",
                        "text": _fmt_pct(t.avg_pct),
                        "color": _pct_color(t.avg_pct),
                        "size": "sm",
                        "flex": 2,
                        "align": "end",
                        "weight": "bold",
                    },
                ],
            }
        )
        if leader_line:
            items.append(
                {
                    "type": "text",
                    "text": leader_line[:40],
                    "color": _COLOR_MUTED,
                    "size": "xxs",
                    "wrap": True,
                }
            )
    return items


def _stocks_list(title: str, stocks: List[ProxyStat], limit: int = 5) -> List[dict]:
    items: List[dict] = [
        {
            "type": "text",
            "text": title,
            "color": _COLOR_MUTED,
            "size": "xs",
            "weight": "bold",
        }
    ]
    if not stocks:
        items.append({"type": "text", "text": "（資料不足）", "color": _COLOR_MUTED, "size": "xs"})
        return items
    for s in stocks[:limit]:
        items.append(
            {
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {
                        "type": "text",
                        "text": f"{s.stock_id} {s.name}"[:12],
                        "size": "sm",
                        "color": _COLOR_TEXT,
                        "flex": 5,
                    },
                    {
                        "type": "text",
                        "text": s.industry[:6],
                        "size": "xxs",
                        "color": _COLOR_MUTED,
                        "flex": 3,
                    },
                    {
                        "type": "text",
                        "text": _fmt_pct(s.pct),
                        "size": "sm",
                        "color": _pct_color(s.pct),
                        "weight": "bold",
                        "flex": 3,
                        "align": "end",
                    },
                ],
            }
        )
    return items


def build_briefing_bubble(
    *,
    title: str,
    subtitle: str,
    index_id: str,
    index_bar: Optional[IndexBar],
    index_prev_close: Optional[float],
    digest: MarketDigest,
    hot_stocks_limit: int = 5,
    image_url: Optional[str] = None,
) -> dict:
    body_contents: List[dict] = []
    body_contents.extend(_index_block(index_id, index_bar, index_prev_close))

    if digest.total_members:
        body_contents.append(_separator())
        body_contents.append(
            _kv(
                "proxy 檔數",
                f"{digest.total_members}（漲 {digest.advancers}／跌 {digest.decliners}）",
            )
        )
        body_contents.append(_kv("平均漲跌", _fmt_pct(digest.avg_pct), _pct_color(digest.avg_pct)))
        body_contents.append(_kv("總成交", f"{_fmt_yi(digest.total_turnover)}元"))

    body_contents.append(_separator())
    body_contents.extend(_themes_block("🔥 熱門族群", digest.hot_themes, limit=3))
    if digest.cold_themes:
        body_contents.append(_separator("sm"))
        body_contents.extend(_themes_block("❄️ 偏弱族群", digest.cold_themes, limit=2))

    body_contents.append(_separator())
    body_contents.extend(_stocks_list("▲ 熱門個股", digest.top_gainers, limit=hot_stocks_limit))
    if digest.top_losers:
        body_contents.append(_separator("sm"))
        body_contents.extend(_stocks_list("▼ 弱勢個股", digest.top_losers, limit=max(3, hot_stocks_limit // 2)))

    bubble: dict = {
        "type": "bubble",
        "size": "giga",
        "header": _header(title, subtitle),
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "spacing": "sm",
            "contents": body_contents,
        },
    }
    if image_url:
        bubble["hero"] = {
            "type": "image",
            "url": image_url,
            "size": "full",
            "aspectRatio": "6:7",
            "aspectMode": "cover",
            "action": {"type": "uri", "uri": image_url},
        }
    return bubble
