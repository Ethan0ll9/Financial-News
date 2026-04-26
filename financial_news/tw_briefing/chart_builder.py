"""matplotlib 儀表板 PNG 產生器。

Layout（高 1600px 以內，寬 1200px）：
    ┌────────────────────────────────┐
    │  大盤指數卡（OHLC、%、成交量）  │   row 0 (height 0.18)
    ├────────────────┬───────────────┤
    │ 熱門族群橫條    │ 偏弱族群橫條   │   row 1 (height 0.28)
    ├────────────────┼───────────────┤
    │ 熱門個股 Top N  │ 領跌個股 Top N │   row 2 (height 0.28)
    ├────────────────┴───────────────┤
    │  成交活躍 Top K (水平條)         │   row 3 (height 0.26)
    └────────────────────────────────┘
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402

from financial_news.tw_briefing.finmind_client import IndexBar
from financial_news.tw_briefing.market_queries import ProxyStat
from financial_news.tw_briefing.theme_detect import MarketDigest, ThemeSummary
from financial_news.utils import setup_logger

logger = setup_logger(__name__)

_CJK_FONT_CANDIDATES = [
    "Microsoft JhengHei",
    "Microsoft YaHei",
    "PingFang TC",
    "Heiti TC",
    "SimHei",
    "Noto Sans CJK TC",
    "PMingLiU",
    "MingLiU",
    "Arial Unicode MS",
]

_FONT_READY = False

# 去除 U+1F300 ~ U+1FAFF、U+2600 ~ U+27BF 等 emoji 區段，避免 matplotlib 缺字警告
import re as _re

_EMOJI_RE = _re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F2FF"
    "]",
    flags=_re.UNICODE,
)


def _strip_emoji(s: str) -> str:
    return _EMOJI_RE.sub("", s).strip()

_COLOR_UP = "#d9534f"   # 漲（紅）
_COLOR_DOWN = "#2d8f5a"  # 跌（綠）
_COLOR_FLAT = "#95a5a6"
_COLOR_BG = "#ffffff"
_COLOR_CARD = "#f6f8fb"
_COLOR_TEXT = "#212529"
_COLOR_MUTED = "#6c757d"
_COLOR_ACCENT = "#3559a5"


def _ensure_cjk_font() -> None:
    global _FONT_READY
    if _FONT_READY:
        return
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    chosen: Optional[str] = None
    for cand in _CJK_FONT_CANDIDATES:
        if cand in available:
            chosen = cand
            break
    if chosen:
        matplotlib.rcParams["font.family"] = [chosen, "sans-serif"]
        matplotlib.rcParams["axes.unicode_minus"] = False
        logger.info("matplotlib 中文字型使用 %s", chosen)
    else:
        logger.warning(
            "未找到常見 CJK 字型（候選：%s），圖表中文可能變成方塊",
            ", ".join(_CJK_FONT_CANDIDATES),
        )
    _FONT_READY = True


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _fmt_yi(turnover: int) -> str:
    return f"{turnover / 1e8:,.1f} 億"


def _color_by_pct(p: float) -> str:
    if p > 0.05:
        return _COLOR_UP
    if p < -0.05:
        return _COLOR_DOWN
    return _COLOR_FLAT


@dataclass
class DashboardInput:
    title: str
    subtitle: str
    session_label: str  # e.g., "2026-04-24"
    index_id: str
    index_bar: Optional[IndexBar]
    index_prev_close: Optional[float]
    digest: MarketDigest
    footer: str = ""


def _draw_header(ax, data: DashboardInput) -> None:
    ax.set_facecolor(_COLOR_BG)
    ax.axis("off")

    bar = data.index_bar
    prev = data.index_prev_close

    pct_txt = ""
    pct_color = _COLOR_TEXT
    if bar and prev and prev > 0:
        p = (bar.close - prev) / prev * 100.0
        pct_txt = _fmt_pct(p)
        pct_color = _color_by_pct(p)

    ax.text(
        0.01,
        0.80,
        _strip_emoji(data.title),
        fontsize=22,
        fontweight="bold",
        color=_COLOR_TEXT,
        transform=ax.transAxes,
    )
    ax.text(
        0.01,
        0.55,
        _strip_emoji(data.subtitle),
        fontsize=13,
        color=_COLOR_MUTED,
        transform=ax.transAxes,
    )

    if bar:
        line1 = (
            f"加權指數（{data.index_id}）  收 {bar.close:,.2f}   開 {bar.open:,.2f}   "
            f"高 {bar.high:,.2f}   低 {bar.low:,.2f}"
        )
        line2 = (
            f"成交 {_fmt_yi(bar.trading_money)}元    "
            f"量 {bar.volume / 1e8:,.2f} 億股    "
            f"交易日 {data.session_label}"
        )
        ax.text(0.01, 0.28, line1, fontsize=13, color=_COLOR_TEXT, transform=ax.transAxes)
        ax.text(0.01, 0.08, line2, fontsize=11, color=_COLOR_MUTED, transform=ax.transAxes)
    else:
        ax.text(
            0.01,
            0.28,
            f"加權指數（{data.index_id}）無 K 線資料（建議 TW_INDEX_STOCK_ID=TAIEX）",
            fontsize=12,
            color=_COLOR_MUTED,
            transform=ax.transAxes,
        )

    if pct_txt:
        ax.text(
            0.99,
            0.50,
            pct_txt,
            fontsize=38,
            fontweight="bold",
            color=pct_color,
            transform=ax.transAxes,
            ha="right",
            va="center",
        )

    d = data.digest
    if d.total_members:
        ax.text(
            0.99,
            0.12,
            f"proxy {d.total_members} 檔｜漲 {d.advancers}／平 {d.unchanged}／跌 {d.decliners}　"
            f"均 {_fmt_pct(d.avg_pct)}　總成交 {_fmt_yi(d.total_turnover)}",
            fontsize=10,
            color=_COLOR_MUTED,
            transform=ax.transAxes,
            ha="right",
        )


def _draw_theme_bars(ax, title: str, themes: List[ThemeSummary], *, diverging: bool) -> None:
    ax.set_facecolor(_COLOR_CARD)
    ax.set_title(title, fontsize=13, fontweight="bold", color=_COLOR_TEXT, loc="left", pad=8)
    if not themes:
        ax.text(0.5, 0.5, "（資料不足）", ha="center", va="center", color=_COLOR_MUTED, fontsize=11)
        ax.axis("off")
        return

    # 取前 5（避免擠壓）
    themes = themes[:5]

    labels = [f"{t.industry}（{t.member_count}）" for t in themes]
    values = [t.avg_pct for t in themes]
    colors = [_color_by_pct(v) for v in values] if diverging else [_COLOR_UP] * len(values)
    labels = list(reversed(labels))
    values = list(reversed(values))
    colors = list(reversed(colors))
    themes_r = list(reversed(themes))

    bars = ax.barh(labels, values, color=colors, height=0.55, edgecolor="white")
    ax.axvline(0, color="#c0c4cc", linewidth=0.8)
    # 讓 x 軸兩側留餘裕，放得下 pct + 代表股
    max_abs = max((abs(v) for v in values), default=1.0) or 1.0
    # 正值 bar 的 suffix 可能較長，右邊預留較寬
    right_pad = max_abs * (1.6 if any(v > 0 for v in values) else 0.2)
    left_pad = max_abs * 0.2
    ax.set_xlim(min(min(values), 0) - left_pad, max(max(values), 0) + right_pad)

    for bar, v, t in zip(bars, values, themes_r):
        x = bar.get_width()
        ha = "left" if x >= 0 else "right"
        xtext = x + (max_abs * 0.02 if x >= 0 else -max_abs * 0.02)
        # 僅對正值 bar 附上代表股文字，避免負值 bar 與 y-label 重疊
        leader_suffix = ""
        if x > 0:
            leaders = [s for s in t.leaders(2) if s.name]
            if leaders:
                leader_suffix = "  " + "、".join(f"{s.stock_id}{s.name}" for s in leaders)
        ax.text(
            xtext,
            bar.get_y() + bar.get_height() / 2,
            f"{_fmt_pct(v)}{leader_suffix}",
            va="center",
            ha=ha,
            fontsize=9,
            color=_COLOR_TEXT,
            fontweight="bold",
        )
    ax.grid(axis="x", color="#e9ecef", linewidth=0.5)
    ax.set_xlabel("平均漲跌幅 (%)", fontsize=9, color=_COLOR_MUTED)
    ax.tick_params(axis="x", labelsize=9, colors=_COLOR_MUTED)
    ax.tick_params(axis="y", labelsize=10, colors=_COLOR_TEXT)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color("#dee2e6")
    ax.spines["bottom"].set_color("#dee2e6")


def _draw_stock_bars(ax, title: str, stocks: List[ProxyStat], *, take: int) -> None:
    ax.set_facecolor(_COLOR_CARD)
    ax.set_title(title, fontsize=13, fontweight="bold", color=_COLOR_TEXT, loc="left", pad=8)
    items = stocks[:take]
    if not items:
        ax.text(0.5, 0.5, "（資料不足）", ha="center", va="center", color=_COLOR_MUTED, fontsize=11)
        ax.axis("off")
        return
    labels = [f"{s.stock_id} {s.name}\n{s.industry}" for s in items]
    values = [s.pct for s in items]
    colors = [_color_by_pct(v) for v in values]
    labels = list(reversed(labels))
    values = list(reversed(values))
    colors = list(reversed(colors))

    bars = ax.barh(labels, values, color=colors, height=0.6, edgecolor="white")
    ax.axvline(0, color="#c0c4cc", linewidth=0.8)
    max_abs = max((abs(v) for v in values), default=1.0) or 1.0
    pad = max_abs * 0.25
    ax.set_xlim(min(min(values), 0) - pad, max(max(values), 0) + pad)
    for bar, v in zip(bars, values):
        x = bar.get_width()
        xtext = x + (max_abs * 0.02 if x >= 0 else -max_abs * 0.02)
        ha = "left" if x >= 0 else "right"
        ax.text(
            xtext,
            bar.get_y() + bar.get_height() / 2,
            _fmt_pct(v),
            va="center",
            ha=ha,
            fontsize=10,
            color=_COLOR_TEXT,
            fontweight="bold",
        )
    ax.grid(axis="x", color="#e9ecef", linewidth=0.5)
    ax.set_xlabel("當日漲跌幅 (%)", fontsize=9, color=_COLOR_MUTED)
    ax.tick_params(axis="x", labelsize=9, colors=_COLOR_MUTED)
    ax.tick_params(axis="y", labelsize=9, colors=_COLOR_TEXT)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color("#dee2e6")
    ax.spines["bottom"].set_color("#dee2e6")


def _draw_turnover(ax, title: str, stocks: List[ProxyStat]) -> None:
    ax.set_facecolor(_COLOR_CARD)
    ax.set_title(title, fontsize=13, fontweight="bold", color=_COLOR_TEXT, loc="left", pad=8)
    if not stocks:
        ax.text(0.5, 0.5, "（資料不足）", ha="center", va="center", color=_COLOR_MUTED, fontsize=11)
        ax.axis("off")
        return
    labels = [f"{s.stock_id} {s.name}" for s in stocks]
    values = [s.turnover / 1e8 for s in stocks]
    colors = [_color_by_pct(s.pct) for s in stocks]
    labels = list(reversed(labels))
    values = list(reversed(values))
    colors = list(reversed(colors))
    stocks_r = list(reversed(stocks))

    bars = ax.barh(labels, values, color=colors, height=0.55, edgecolor="white")
    # 右邊多留 35% 給文字
    max_v = max(values) if values else 1.0
    ax.set_xlim(0, max_v * 1.45)
    for bar, v, s in zip(bars, values, stocks_r):
        x = bar.get_width()
        ax.text(
            x + max_v * 0.01,
            bar.get_y() + bar.get_height() / 2,
            f"{v:,.1f} 億｜收 {s.close:,.2f}｜{_fmt_pct(s.pct)}｜{s.industry}",
            va="center",
            ha="left",
            fontsize=9,
            color=_COLOR_TEXT,
        )
    ax.grid(axis="x", color="#e9ecef", linewidth=0.5)
    ax.set_xlabel("成交金額（億元）", fontsize=9, color=_COLOR_MUTED)
    ax.tick_params(axis="x", labelsize=9, colors=_COLOR_MUTED)
    ax.tick_params(axis="y", labelsize=8, colors=_COLOR_TEXT)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color("#dee2e6")
    ax.spines["bottom"].set_color("#dee2e6")


def build_dashboard_figure(data: DashboardInput) -> Figure:
    _ensure_cjk_font()
    fig = plt.figure(figsize=(12, 17.0), dpi=110, facecolor=_COLOR_BG)
    gs = GridSpec(
        nrows=4,
        ncols=2,
        figure=fig,
        height_ratios=[0.8, 1.7, 1.8, 2.4],
        hspace=0.60,
        wspace=0.32,
        left=0.12,
        right=0.97,
        top=0.96,
        bottom=0.05,
    )

    ax_header = fig.add_subplot(gs[0, :])
    _draw_header(ax_header, data)

    ax_hot = fig.add_subplot(gs[1, 0])
    _draw_theme_bars(ax_hot, "熱門族群（綜合漲幅 + 成交量）", data.digest.hot_themes, diverging=True)

    ax_cold = fig.add_subplot(gs[1, 1])
    _draw_theme_bars(ax_cold, "偏弱族群（平均漲幅）", data.digest.cold_themes, diverging=True)

    ax_up = fig.add_subplot(gs[2, 0])
    _draw_stock_bars(ax_up, "熱門個股 · 漲幅 Top", data.digest.top_gainers, take=len(data.digest.top_gainers))

    ax_down = fig.add_subplot(gs[2, 1])
    _draw_stock_bars(ax_down, "弱勢個股 · 跌幅 Top", data.digest.top_losers, take=len(data.digest.top_losers))

    ax_to = fig.add_subplot(gs[3, :])
    _draw_turnover(ax_to, "成交活躍 Top", data.digest.top_turnover)

    if data.footer:
        fig.text(
            0.98,
            0.01,
            data.footer,
            fontsize=8,
            color=_COLOR_MUTED,
            ha="right",
            va="bottom",
        )
    return fig


def render_dashboard_png(data: DashboardInput, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig = build_dashboard_figure(data)
    try:
        fig.savefig(out_path, format="png", facecolor=fig.get_facecolor())
    finally:
        plt.close(fig)
    logger.info("儀表板 PNG 已寫入 %s (%d bytes)", out_path, os.path.getsize(out_path))
    return out_path
