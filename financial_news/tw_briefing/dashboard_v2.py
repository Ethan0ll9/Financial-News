"""新版台股儀表板：兩張 PNG（overview / watchlist），同時供 LINE 與 Telegram 推送。

設計目標
---------
- ``render_overview_png``：4 大指數卡（含漲跌家數環圈）＋ 全市場產業資金淨流入／流出熱力圖
- ``render_watchlist_png``：使用者觀察清單迷你熱力圖 ＋ 強勢／弱勢族群清單

兩張圖都是「自包含」訊息（不再依賴 Flex 卡片），LINE 與 Telegram 收到的內容完全相同。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import squarify  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from financial_news.core.utils import setup_logger
from financial_news.tw_briefing.formatting import strip_emoji as _strip_emoji
from financial_news.tw_briefing.market_breadth import IndexQuote, IndustryFlow
from financial_news.tw_briefing.market_queries import ProxyStat
from financial_news.tw_briefing.theme_detect import MarketDigest, ThemeSummary

logger = setup_logger(__name__)


# ---- 配色（台股慣例：紅漲綠跌） --------------------------------------------

_COLOR_BG = "#ffffff"
_COLOR_CARD = "#f7f8fa"
_COLOR_TEXT = "#1f2937"
_COLOR_MUTED = "#6b7280"
_COLOR_BORDER = "#e5e7eb"
_COLOR_UP = "#d9534f"
_COLOR_DOWN = "#2d8f5a"
_COLOR_FLAT = "#9ca3af"

# 觀察清單迷你熱力專用（含中性灰）
_WATCH_NEUTRAL = "#bcc3cc"


# ---- 離散色階（紅漲綠跌：飽和但不沉悶）-----------------------------------
#  靠近 0% (|pct| < 0.5%) 視為「平盤」用灰，其餘對應紅綠各 4 級
_HEATMAP_STOPS = [
    (3.0, "#d92e26"),   # +3% 以上：飽和正紅
    (2.0, "#e8534a"),   # +2~+3%
    (1.0, "#f08079"),   # +1~+2%
    (0.5, "#f9b8b4"),   # +0.5~+1%
    (-0.5, "#bcc3cc"),  # |pct| < 0.5%：灰（平盤）
    (-1.0, "#b3dec1"),  # -0.5~-1%
    (-2.0, "#7ec99a"),  # -1~-2%
    (-3.0, "#3eaf72"),  # -2~-3%
]
_HEATMAP_FLOOR = "#1f8c54"  # < -3%：飽和深綠


def _heatmap_color(pct: float) -> str:
    """離散階梯式配色：每 ~1% 一階；|pct| < 0.5% 視為灰色平盤。"""
    for threshold, color in _HEATMAP_STOPS:
        if pct >= threshold:
            return color
    return _HEATMAP_FLOOR


def _is_dark_background(pct: float) -> bool:
    """根據 pct 階梯判斷背景是否偏深（用以決定字色）。

    新色階：|pct| >= 2% 為深色背景 → 白字；其餘淺色背景 → 深字。
    """
    return abs(pct) >= 2.0


def _fit_text(text: str, max_chars: int) -> str:
    """中文字串截斷至 max_chars 字（含尾省略號）。"""
    if max_chars <= 1:
        return text[:1]
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _fmt_money_smart(money: int) -> str:
    """金額智慧顯示：>=1 兆用「兆」、其餘用「億」。"""
    yi = money / 1e8
    if yi >= 10000:
        return f"{yi / 10000:.2f} 兆"
    if yi >= 1000:
        return f"{yi:,.0f} 億"
    if yi >= 100:
        return f"{yi:,.1f} 億"
    return f"{yi:,.2f} 億"


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
            "未找到常見 CJK 字型（候選：%s），中文可能變方塊",
            ", ".join(_CJK_FONT_CANDIDATES),
        )
    _FONT_READY = True


def _color_for_pct(pct: float, *, threshold: float = 0.05) -> str:
    if pct > threshold:
        return _COLOR_UP
    if pct < -threshold:
        return _COLOR_DOWN
    return _COLOR_FLAT


def _fmt_yi(money: int, decimals: int = 1) -> str:
    """元 → 億字串。"""
    yi = money / 1e8
    return f"{yi:,.{decimals}f} 億"


def _fmt_pct(p: float) -> str:
    sign = "+" if p > 0 else ("" if p == 0 else "")
    return f"{sign}{p:.2f}%"


# ---- 圖①：Overview（指數 + 雙產業熱力） -----------------------------------


@dataclass
class OverviewData:
    title: str
    subtitle: str
    session_label: str
    indices: List[IndexQuote]
    inflow: List[IndustryFlow]
    outflow: List[IndustryFlow]
    footer: str = ""


def _draw_title(ax, title: str, subtitle: str) -> None:
    ax.axis("off")
    ax.set_facecolor(_COLOR_BG)
    ax.text(
        0.01, 0.72, _strip_emoji(title),
        fontsize=22, fontweight="bold", color=_COLOR_TEXT, transform=ax.transAxes,
    )
    ax.text(
        0.01, 0.32, _strip_emoji(subtitle),
        fontsize=13, color=_COLOR_MUTED, transform=ax.transAxes,
    )


def _draw_index_card(ax, idx: IndexQuote) -> None:
    """單張指數卡：環圈（漲跌平） + 收盤 + 點數 + %（拆兩行，避免跑版）。"""
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_aspect("auto")
    ax.axis("off")
    ax.set_facecolor(_COLOR_BG)

    ax.add_patch(
        Rectangle(
            (0.02, 0.05), 0.96, 0.90,
            facecolor=_COLOR_CARD, edgecolor=_COLOR_BORDER,
            linewidth=1.2, zorder=0,
        )
    )

    ax.text(0.06, 0.84, idx.label, fontsize=13, fontweight="bold", color=_COLOR_TEXT)

    # ---- 環圈（漲/跌/平家數）：用 inset_axes + aspect=equal 確保正圓且夠大 ----
    total = max(idx.total, 1)
    values = [idx.advancers, idx.decliners, idx.unchanged]
    colors = [_COLOR_UP, _COLOR_DOWN, _COLOR_FLAT]
    from matplotlib.patches import Wedge

    # bounds = [x0, y0, w, h]（皆為父 axes 比例）；aspect=equal 會在此範圍內畫正圓
    donut = ax.inset_axes([0.04, 0.08, 0.44, 0.84])
    donut.set_xlim(-1.1, 1.1)
    donut.set_ylim(-1.1, 1.1)
    donut.set_aspect("equal")
    donut.set_facecolor(_COLOR_CARD)
    donut.axis("off")

    # 環圈改薄（inner=0.82）：宛如細圓圈，數字在圓內主體區域更顯眼
    radius, inner = 1.0, 0.82
    theta = 90.0
    for v, c in zip(values, colors):
        if v <= 0:
            continue
        span = v / total * 360.0
        donut.add_patch(
            Wedge(
                center=(0, 0), r=radius,
                theta1=theta - span, theta2=theta,
                facecolor=c, edgecolor="white", linewidth=0.8,
                width=radius - inner,
            )
        )
        theta -= span

    donut.text(0, 0.32, f"漲 {idx.advancers}", fontsize=9, color=_COLOR_UP,
               ha="center", va="center", fontweight="bold")
    donut.text(0, 0.00, f"跌 {idx.decliners}", fontsize=9, color=_COLOR_DOWN,
               ha="center", va="center", fontweight="bold")
    # 「平」字色改深（用 _COLOR_TEXT 深灰），避免淺灰看不清
    donut.text(0, -0.32, f"平 {idx.unchanged}", fontsize=9, color=_COLOR_TEXT,
               ha="center", va="center", fontweight="bold")

    # ---- 右側：收盤（上）+ 點數 % 並排（下）---------------------------------
    # 指數卡的數字色：sign-based（只看正負），避免「-0.11% 顯示灰色」反直覺
    if idx.pct > 0:
        pct_color = _COLOR_UP
    elif idx.pct < 0:
        pct_color = _COLOR_DOWN
    else:
        pct_color = _COLOR_FLAT

    close_text = f"{idx.close:,.2f}" if idx.close > 0 else "—"
    close_fontsize = 20 if len(close_text) <= 6 else 17
    ax.text(
        0.96, 0.60, close_text,
        fontsize=close_fontsize, fontweight="bold", color=_COLOR_TEXT, ha="right",
    )

    diff_sign = "+" if idx.diff > 0 else ""
    diff_text = f"{diff_sign}{idx.diff:,.2f}" if (idx.close > 0 and idx.diff != 0) else ""
    pct_sign = "+" if idx.pct > 0 else ""
    pct_text = f"{pct_sign}{idx.pct:.2f}%"

    # 漲跌點 + 漲跌幅 並排顯示（同一行）
    combined = f"{diff_text}  {pct_text}" if diff_text else pct_text
    ax.text(
        0.96, 0.28, combined,
        fontsize=12, fontweight="bold", color=pct_color, ha="right",
    )


def _draw_treemap(
    ax,
    title: str,
    flows: List[IndustryFlow],
) -> None:
    """產業 treemap：size = 產業總市值，color = 階梯式漲跌幅階梯。

    - **依市值由大到小排序** → 大塊在左上（仿台股 heatmap 範例）
    - **離散色階**：-3%~+3% 七階梯；接近 0% 用灰色平盤
    - **字體自適應**：方塊小時只顯示縮寫、不會跑版
    """
    ax.set_facecolor(_COLOR_BG)
    ax.set_title(title, fontsize=14, fontweight="bold", color=_COLOR_TEXT, loc="left", pad=10)

    if not flows:
        ax.text(0.5, 0.5, "（無資料）", ha="center", va="center",
                color=_COLOR_MUTED, fontsize=12, transform=ax.transAxes)
        ax.axis("off")
        return

    # 依市值由大到小（市值為 0 退回 turnover 排序）
    ordered = sorted(flows, key=lambda f: (-(f.market_cap or 0), -f.turnover))

    # treemap 視覺：用 sqrt(market_cap) 而非真實 market_cap 來分配方塊面積。
    # 真實市值差距常達 100 倍以上（半導體 77 兆 vs 玻璃陶瓷 千億級），
    # 直接照比例會讓邊緣產業變得極小、字無法顯示。sqrt 是「面積版」開根號，
    # 視覺上保留大小關係但邊緣產業仍可讀。
    sizes = [max((f.market_cap or f.turnover or 1) ** 0.5, 1.0) for f in ordered]

    norm_sizes = squarify.normalize_sizes(sizes, 100, 100)
    rects = squarify.squarify(norm_sizes, 0, 0, 100, 100)

    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.set_aspect("auto")
    ax.invert_yaxis()  # (0,0) 在左上
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)

    for f, r in zip(ordered, rects):
        x, y, w, h = r["x"], r["y"], r["dx"], r["dy"]
        face = _heatmap_color(f.avg_pct)
        ax.add_patch(
            Rectangle(
                (x, y), w, h,
                facecolor=face,
                edgecolor="white",
                linewidth=2.5,
            )
        )
        text_color = "white" if _is_dark_background(f.avg_pct) else "#1f2937"
        _label_treemap_cell(ax, f, x, y, w, h, text_color=text_color)


def _label_treemap_cell(
    ax,
    f: IndustryFlow,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    text_color: str = "white",
) -> None:
    """依方塊大小決定文字字級／行數，並截斷產業名避免溢出。

    座標系為 [0, 100] × [0, 100]；面積 ``area = w * h``。
    """
    area = w * h
    if area < 2.0:
        return  # 太小：不畫文字（保持顏色塊作 heatmap）

    money = f.market_cap if (f.market_cap or 0) > 0 else f.turnover

    # 中文寬度估算：1 個字大約佔 width 2.6 單位（座標系 [0,100]）
    cw = 2.6
    max_chars_by_w = max(1, int(w / cw))

    # 窄條（w < 7）：只放 1~2 字的產業縮寫，或省略
    if w < 5.0:
        if w < 3.0 or h < 2.0:
            return
        industry = _fit_text(f.industry, max(1, max_chars_by_w))
        ax.text(
            x + w / 2, y + h / 2,
            industry,
            ha="center", va="center", color=text_color,
            fontsize=7, fontweight="bold",
        )
        return

    industry = _fit_text(f.industry, max_chars_by_w)

    # 小（w 5~9 或 area < 18）：產業名 + %
    if area < 18 or w < 9:
        name_size = min(10, max(7.5, w / 1.1))
        pct_size = max(7, name_size - 1.5)
        ax.text(
            x + w / 2, y + h / 2 - h * 0.14,
            industry,
            ha="center", va="center", color=text_color,
            fontsize=name_size, fontweight="bold",
        )
        ax.text(
            x + w / 2, y + h / 2 + h * 0.18,
            _fmt_pct(f.avg_pct),
            ha="center", va="center", color=text_color,
            fontsize=pct_size, fontweight="bold",
        )
        return

    # 中（area 18~50）：產業名 + 金額 + %（緊湊）
    if area < 50:
        name_size = min(12, max(9, w / 1.3))
        money_size = max(8, name_size - 2)
        pct_size = max(8, name_size - 1)
        ax.text(
            x + w / 2, y + h / 2 - h * 0.24,
            industry,
            ha="center", va="center", color=text_color,
            fontsize=name_size, fontweight="bold",
        )
        ax.text(
            x + w / 2, y + h / 2 + h * 0.02,
            _fmt_money_smart(money),
            ha="center", va="center", color=text_color, fontsize=money_size,
        )
        ax.text(
            x + w / 2, y + h / 2 + h * 0.26,
            _fmt_pct(f.avg_pct),
            ha="center", va="center", color=text_color,
            fontsize=pct_size, fontweight="bold",
        )
        return

    # 大（area >= 50）：產業名 + 金額 + %（寬鬆排版，大字級）
    name_size = min(18, max(12, area ** 0.5 / 1.5))
    money_size = max(10, name_size - 4)
    pct_size = max(11, name_size - 3)
    ax.text(
        x + w / 2, y + h / 2 - h * 0.22,
        industry,
        ha="center", va="center", color=text_color,
        fontsize=name_size, fontweight="bold",
    )
    ax.text(
        x + w / 2, y + h / 2 + h * 0.02,
        _fmt_money_smart(money),
        ha="center", va="center", color=text_color, fontsize=money_size,
    )
    ax.text(
        x + w / 2, y + h / 2 + h * 0.26,
        _fmt_pct(f.avg_pct),
        ha="center", va="center", color=text_color,
        fontsize=pct_size, fontweight="bold",
    )


def _draw_heatmap_legend(ax) -> None:
    """畫一條離散色階圖例：-3% / -2% / -1% / 0% / +1% / +2% / +3%。"""
    ax.set_facecolor(_COLOR_BG)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 10)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.axis("off")

    labels = ["-3%", "-2%", "-1%", "0%", "+1%", "+2%", "+3%"]
    pcts = [-3.5, -2.5, -1.5, 0.0, 1.5, 2.5, 3.5]
    n = len(labels)
    bw = 100 / (n + 0.5)
    pad = bw / 4
    x0 = 0.5

    ax.text(
        x0, 5,
        "漲跌幅",
        ha="left", va="center", color=_COLOR_MUTED, fontsize=10,
    )
    base_x = 16
    for i, (lbl, pct) in enumerate(zip(labels, pcts)):
        x = base_x + i * bw
        face = _heatmap_color(pct)
        ax.add_patch(Rectangle((x, 1.0), bw - pad, 5.0, facecolor=face, edgecolor="white", linewidth=1))
        ax.text(
            x + (bw - pad) / 2, -1,
            lbl,
            ha="center", va="top", color=_COLOR_TEXT, fontsize=9,
        )


def build_overview_figure(data: OverviewData) -> Figure:
    _ensure_cjk_font()
    fig = plt.figure(figsize=(12, 17.0), dpi=110, facecolor=_COLOR_BG)
    gs = GridSpec(
        nrows=5,
        ncols=4,
        figure=fig,
        height_ratios=[0.55, 1.9, 4.3, 4.3, 0.45],
        hspace=0.30,
        wspace=0.18,
        left=0.045,
        right=0.97,
        top=0.965,
        bottom=0.03,
    )

    ax_title = fig.add_subplot(gs[0, :])
    _draw_title(ax_title, data.title, data.subtitle)

    indices = list(data.indices)
    while len(indices) < 4:
        indices.append(IndexQuote("—", 0.0, 0.0, 0.0, 0, 0, 0))

    for i in range(4):
        ax_card = fig.add_subplot(gs[1, i])
        _draw_index_card(ax_card, indices[i])

    ax_in = fig.add_subplot(gs[2, :])
    _draw_treemap(ax_in, "產業資金淨流入（方塊大小＝產業總市值）", data.inflow)

    ax_out = fig.add_subplot(gs[3, :])
    _draw_treemap(ax_out, "產業資金淨流出（方塊大小＝產業總市值）", data.outflow)

    ax_legend = fig.add_subplot(gs[4, :])
    _draw_heatmap_legend(ax_legend)

    if data.footer:
        fig.text(0.99, 0.005, data.footer, fontsize=8,
                 color=_COLOR_MUTED, ha="right", va="bottom")

    return fig


def render_overview_png(data: OverviewData, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig = build_overview_figure(data)
    try:
        fig.savefig(out_path, format="png", facecolor=fig.get_facecolor())
    finally:
        plt.close(fig)
    logger.info("Overview PNG → %s (%d bytes)", out_path, os.path.getsize(out_path))
    return out_path


# ---- 圖②：Watchlist（觀察族群熱力 + 強弱清單） ------------------------------


@dataclass
class WatchlistData:
    title: str
    subtitle: str
    digest: MarketDigest
    proxy_stats: List[ProxyStat]
    footer: str = ""


def _draw_watchlist_mini_heatmap(
    ax,
    stats: List[ProxyStat],
    *,
    top_n: int = 30,
) -> None:
    """觀察清單迷你熱力圖：size = 成交金額（Top N），color = 階梯漲跌幅。"""
    ax.set_facecolor(_COLOR_BG)
    ax.set_title(
        f"觀察清單熱力（成交金額 Top {top_n}；色階同 overview）",
        fontsize=13, fontweight="bold", color=_COLOR_TEXT, loc="left", pad=8,
    )
    if not stats:
        ax.text(0.5, 0.5, "（無資料）", ha="center", va="center",
                color=_COLOR_MUTED, fontsize=11, transform=ax.transAxes)
        ax.axis("off")
        return

    ranked = sorted(stats, key=lambda s: -s.turnover)[:top_n]
    sizes = [max(s.turnover, 1) for s in ranked]
    norm_sizes = squarify.normalize_sizes(sizes, 100, 100)
    rects = squarify.squarify(norm_sizes, 0, 0, 100, 100)

    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.invert_yaxis()
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)

    for s, r in zip(ranked, rects):
        x, y, w, h = r["x"], r["y"], r["dx"], r["dy"]
        face = _heatmap_color(s.pct)
        text_color = "white" if _is_dark_background(s.pct) else "#1f2937"
        ax.add_patch(
            Rectangle(
                (x, y), w, h,
                facecolor=face, edgecolor="white", linewidth=2.0,
            )
        )
        area = w * h
        if area < 2.5:
            continue
        # 個股名稱可較長（如「電腦及週邊」），截斷到 box 寬度允許範圍
        max_chars = max(2, int(w / 2.0))
        label = _fit_text(f"{s.stock_id} {s.name}".strip(), max_chars + 5)
        if area < 8:
            ax.text(x + w / 2, y + h / 2, s.stock_id,
                    ha="center", va="center", color=text_color,
                    fontsize=7, fontweight="bold")
            continue
        name_size = min(14, max(7.5, area ** 0.5 / 2.0))
        pct_size = max(7.5, name_size - 1.5)
        ax.text(
            x + w / 2, y + h / 2 - h * 0.18,
            label,
            ha="center", va="center", color=text_color,
            fontsize=name_size, fontweight="bold",
        )
        ax.text(
            x + w / 2, y + h / 2 + h * 0.22,
            _fmt_pct(s.pct),
            ha="center", va="center", color=text_color,
            fontsize=pct_size, fontweight="bold",
        )


def _draw_theme_panel(
    ax,
    title: str,
    themes: List[ThemeSummary],
    *,
    direction: str,  # "strong" | "weak"
) -> None:
    """強勢／弱勢族群清單面板：族群名（平均%）+ 成員代號／名稱／%。"""
    ax.set_facecolor(_COLOR_CARD)
    ax.set_title(title, fontsize=13, fontweight="bold", color=_COLOR_TEXT, loc="left", pad=8)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.invert_yaxis()
    ax.axis("off")

    if not themes:
        ax.text(0.5, 0.5, "（資料不足）", ha="center", va="center",
                color=_COLOR_MUTED, fontsize=11)
        return

    # 取前 5 群
    panel_themes = themes[:5]
    y = 0.06
    step = 0.95 / (len(panel_themes) * 4.4)  # 預留標題 + 4 行內容

    for t in panel_themes:
        # 族群標頭：背景色條
        avg_color = _COLOR_UP if direction == "strong" else _COLOR_DOWN
        ax.add_patch(
            Rectangle((0.02, y - 0.015), 0.96, step * 1.1,
                      facecolor=avg_color, alpha=0.10,
                      edgecolor=avg_color, linewidth=0.6)
        )
        ax.text(
            0.04, y + step * 0.5,
            f"{t.industry}（{t.member_count}）",
            fontsize=11, fontweight="bold", color=_COLOR_TEXT,
            va="center",
        )
        ax.text(
            0.96, y + step * 0.5,
            _fmt_pct(t.avg_pct),
            fontsize=11, fontweight="bold", color=avg_color,
            ha="right", va="center",
        )
        y += step * 1.2

        # 成員：sort by pct（強勢 desc / 弱勢 asc），取前 4
        members = list(t.members)
        members.sort(key=lambda s: -s.pct if direction == "strong" else s.pct)
        for s in members[:4]:
            row_color = _color_for_pct(s.pct)
            ax.text(
                0.06, y + step * 0.5,
                f"{s.stock_id}  {s.name}",
                fontsize=10, color=_COLOR_TEXT, va="center",
            )
            ax.text(
                0.96, y + step * 0.5,
                _fmt_pct(s.pct),
                fontsize=10, fontweight="bold", color=row_color,
                ha="right", va="center",
            )
            y += step * 0.85
        y += step * 0.5  # 群間留白


def build_watchlist_figure(data: WatchlistData) -> Figure:
    _ensure_cjk_font()
    fig = plt.figure(figsize=(12, 14.5), dpi=110, facecolor=_COLOR_BG)
    gs = GridSpec(
        nrows=3,
        ncols=2,
        figure=fig,
        height_ratios=[0.55, 4.0, 4.5],
        hspace=0.28,
        wspace=0.10,
        left=0.045,
        right=0.97,
        top=0.965,
        bottom=0.03,
    )

    ax_title = fig.add_subplot(gs[0, :])
    _draw_title(ax_title, data.title, data.subtitle)

    ax_heat = fig.add_subplot(gs[1, :])
    _draw_watchlist_mini_heatmap(ax_heat, data.proxy_stats)

    ax_strong = fig.add_subplot(gs[2, 0])
    _draw_theme_panel(ax_strong, "強勢族群（觀察清單）", data.digest.hot_themes, direction="strong")

    ax_weak = fig.add_subplot(gs[2, 1])
    _draw_theme_panel(ax_weak, "弱勢族群（觀察清單）", data.digest.cold_themes, direction="weak")

    if data.footer:
        fig.text(0.99, 0.005, data.footer, fontsize=8,
                 color=_COLOR_MUTED, ha="right", va="bottom")

    return fig


def render_watchlist_png(data: WatchlistData, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig = build_watchlist_figure(data)
    try:
        fig.savefig(out_path, format="png", facecolor=fig.get_facecolor())
    finally:
        plt.close(fig)
    logger.info("Watchlist PNG → %s (%d bytes)", out_path, os.path.getsize(out_path))
    return out_path
