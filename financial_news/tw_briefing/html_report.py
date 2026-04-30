"""本機 HTML 儀表板產生器（不依賴外部 CSS/JS CDN；全部 inline）。"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from financial_news.tw_briefing.finmind_client import IndexBar
from financial_news.tw_briefing.market_queries import ProxyStat
from financial_news.tw_briefing.theme_detect import MarketDigest, ThemeSummary
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)

_COLOR_UP = "#d9534f"
_COLOR_DOWN = "#2d8f5a"
_COLOR_FLAT = "#868e96"


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def text_block_to_html(text: str) -> str:
    """把純文字段落轉成 HTML 可顯示的預格式化區塊（保留換行與全形空白）。

    供 premarket_report / postmarket_report 共用，避免兩處重複定義。
    """
    return (
        '<pre style="font-family:inherit;font-size:12px;color:#374151;'
        f'white-space:pre-wrap;margin:0">{html.escape(text)}</pre>'
    )


from financial_news.tw_briefing.formatting import (
    fmt_pct as _fmt_pct,
    fmt_yi as _fmt_yi_base,
    pct_class_name as _pct_class,
)


def _fmt_yi(money: int) -> str:
    """html 使用 2 位小數（與舊版一致）。"""
    return _fmt_yi_base(money, decimals=2)


def _bar_svg(values: List[float], max_abs: float, *, width: int = 220, height: int = 14) -> str:
    if max_abs <= 0:
        max_abs = 1.0
    mid = width / 2
    svg_parts = [f'<svg width="{width}" height="{height * len(values)}" xmlns="http://www.w3.org/2000/svg">']
    for i, v in enumerate(values):
        y = i * height + 2
        bar_w = min(abs(v) / max_abs, 1.0) * (width / 2 - 2)
        if v >= 0:
            x = mid
            color = _COLOR_UP
        else:
            x = mid - bar_w
            color = _COLOR_DOWN
        svg_parts.append(
            f'<rect x="{x:.1f}" y="{y}" width="{bar_w:.1f}" height="{height - 4}" fill="{color}" rx="2"></rect>'
        )
    svg_parts.append(f'<line x1="{mid}" y1="0" x2="{mid}" y2="{height * len(values)}" stroke="#dee2e6" stroke-width="1"/>')
    svg_parts.append("</svg>")
    return "".join(svg_parts)


@dataclass
class HtmlReportData:
    title: str
    subtitle: str
    session_label: str
    index_id: str
    index_bar: Optional[IndexBar]
    index_prev_close: Optional[float]
    digest: MarketDigest
    generated_at: str
    extra_sections: List[dict] = field(default_factory=list)
    image_url: Optional[str] = None  # imgbb URL (若已上傳)
    footer: str = ""


_CSS = """
:root {
  --up: #d9534f; --down: #2d8f5a; --flat: #868e96;
  --bg: #f4f6fa; --card: #ffffff; --text: #1e293b; --muted: #64748b; --accent: #3559a5;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
             font-family: "Microsoft JhengHei", "PingFang TC", "Noto Sans TC", "Helvetica Neue", Arial, sans-serif; }
.container { max-width: 1100px; margin: 0 auto; padding: 20px 18px 60px; }
.header { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px;
          padding: 16px 20px; background: linear-gradient(135deg, #3559a5, #5a7fd4); border-radius: 16px;
          color: white; box-shadow: 0 8px 24px rgba(53,89,165,0.15); }
.header h1 { margin: 0; font-size: 22px; }
.header .subtitle { margin-top: 4px; opacity: .85; font-size: 13px; }
.card { background: var(--card); border-radius: 14px; padding: 16px 18px; margin-top: 16px;
        box-shadow: 0 2px 8px rgba(15,23,42,0.04); }
.card h2 { margin: 0 0 12px 0; font-size: 15px; color: var(--text);
           border-left: 4px solid var(--accent); padding-left: 10px; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 780px) { .row { grid-template-columns: 1fr; } }
.up { color: var(--up); } .down { color: var(--down); } .flat { color: var(--flat); }
.big-pct { font-size: 40px; font-weight: 700; line-height: 1.1; }
.big-pts { font-size: 18px; font-weight: 600; margin-top: 2px; }
.stat-line { font-size: 13px; color: var(--muted); margin-top: 4px; }
.ohlc { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 10px; }
.ohlc > div { background: #f8f9fc; padding: 10px 12px; border-radius: 10px; }
.ohlc .lab { font-size: 12px; color: var(--muted); }
.ohlc .val { font-size: 18px; font-weight: 600; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 10px; text-align: left; font-size: 13px; border-bottom: 1px solid #eef1f5; }
th { background: #f8f9fc; color: var(--muted); font-weight: 500; }
tbody tr:hover { background: #f8fafc; }
.chip { display: inline-block; background: #eef2ff; color: #3f51b5; font-size: 11px; padding: 2px 8px;
        border-radius: 999px; margin-left: 6px; }
.theme-block { padding: 10px 12px; border-radius: 12px; margin-bottom: 10px; background: #f8fbff; }
.theme-block .ti { display: flex; justify-content: space-between; align-items: baseline; }
.theme-block .ti .ind { font-weight: 600; font-size: 14px; }
.theme-block .ti .pct { font-weight: 700; font-size: 15px; }
.theme-block .leaders { margin-top: 4px; font-size: 12px; color: var(--muted); }
.turnover-row { display: grid; grid-template-columns: 40px 1fr 1fr auto; gap: 10px; align-items: center;
                padding: 6px 10px; border-bottom: 1px solid #eef1f5; }
.turnover-row .rank { font-weight: 700; color: var(--muted); }
.turnover-row .name { font-weight: 500; }
.turnover-row .bar { height: 8px; background: #eef1f5; border-radius: 4px; overflow: hidden; }
.turnover-row .bar > span { display: block; height: 100%; }
.cover-img { width: 100%; border-radius: 12px; margin-top: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
.footer { margin-top: 22px; text-align: center; font-size: 12px; color: var(--muted); }
"""


def _render_header(data: HtmlReportData) -> str:
    bar = data.index_bar
    prev = data.index_prev_close
    pct_html = ""
    pts_html = ""
    if bar and prev and prev > 0:
        p = (bar.close - prev) / prev * 100.0
        diff = bar.close - prev
        cls = _pct_class(p)
        pct_html = f'<div class="big-pct {cls}">{_fmt_pct(p)}</div>'
        sign = "+" if diff >= 0 else ""
        pts_html = (
            f'<div class="big-pts {cls}">{sign}{diff:,.2f} 點</div>'
        )

    right = (pct_html + pts_html) or '<div class="big-pct flat">—</div>'
    d = data.digest
    stat_line = (
        f"proxy {d.total_members} 檔｜漲 {d.advancers}／平 {d.unchanged}／跌 {d.decliners}　"
        f"均 {_fmt_pct(d.avg_pct)}　總成交 {_fmt_yi(d.total_turnover)}"
    ) if d.total_members else ""

    return f'''<div class="header">
      <div>
        <h1>{_esc(data.title)}</h1>
        <div class="subtitle">{_esc(data.subtitle)}</div>
        <div class="subtitle">交易日 {_esc(data.session_label)}｜加權指數（{_esc(data.index_id)}）</div>
      </div>
      <div style="text-align:right">
        {right}
        <div class="stat-line">{_esc(stat_line)}</div>
      </div>
    </div>'''


def _render_index_card(data: HtmlReportData) -> str:
    bar = data.index_bar
    if not bar:
        return f'''<div class="card"><h2>大盤指數</h2>
          <div class="stat-line">（無法取得加權指數 K 線；建議 TW_INDEX_STOCK_ID=TAIEX）</div></div>'''
    ohlc = (
        f'<div class="ohlc">'
        f'<div><div class="lab">開盤</div><div class="val">{bar.open:,.2f}</div></div>'
        f'<div><div class="lab">最高</div><div class="val">{bar.high:,.2f}</div></div>'
        f'<div><div class="lab">最低</div><div class="val">{bar.low:,.2f}</div></div>'
        f'<div><div class="lab">收盤</div><div class="val">{bar.close:,.2f}</div></div>'
        f'</div>'
    )
    if bar.trading_money or bar.volume:
        vol = (
            f"成交 {_fmt_yi(bar.trading_money)}元｜量 {bar.volume / 1e8:,.2f} 億股"
        )
    else:
        vol = "成交 / 量（FinMind 未回傳，請見 FMTQIK 補充）"
    return f'''<div class="card"><h2>大盤指數 · OHLC</h2>
      {ohlc}
      <div class="stat-line" style="margin-top:10px">{_esc(vol)}</div>
    </div>'''


def _render_theme_block(t: ThemeSummary) -> str:
    cls = _pct_class(t.avg_pct)
    leaders = t.leaders(4)
    leader_html = "、".join(
        f'{_esc(s.stock_id)} {_esc(s.name)} <span class="{_pct_class(s.pct)}">{_fmt_pct(s.pct)}</span>'
        for s in leaders if s.name
    )
    return f'''<div class="theme-block">
      <div class="ti">
        <div class="ind">{_esc(t.industry)} <span class="chip">{t.member_count} 檔</span></div>
        <div class="pct {cls}">{_fmt_pct(t.avg_pct)}</div>
      </div>
      <div class="leaders">成交合計 {_fmt_yi(t.total_turnover)}元｜代表：{leader_html}</div>
    </div>'''


def _render_themes_cards(data: HtmlReportData) -> str:
    hot = "".join(_render_theme_block(t) for t in data.digest.hot_themes) or '<div class="stat-line">（資料不足）</div>'
    cold = "".join(_render_theme_block(t) for t in data.digest.cold_themes) or '<div class="stat-line">（資料不足）</div>'
    return f'''<div class="row">
      <div class="card"><h2>熱門族群（綜合漲幅 + 成交量）</h2>{hot}</div>
      <div class="card"><h2>偏弱族群（平均漲幅）</h2>{cold}</div>
    </div>'''


def _render_stocks_table(stocks: List[ProxyStat], title: str) -> str:
    if not stocks:
        return f'<div class="card"><h2>{_esc(title)}</h2><div class="stat-line">（資料不足）</div></div>'
    rows = "".join(
        f'<tr>'
        f'<td>{_esc(s.stock_id)}</td>'
        f'<td>{_esc(s.name)}</td>'
        f'<td><span class="chip">{_esc(s.industry)}</span></td>'
        f'<td class="{_pct_class(s.pct)}" style="text-align:right;font-weight:600">{_fmt_pct(s.pct)}</td>'
        f'<td style="text-align:right">{s.close:,.2f}</td>'
        f'<td style="text-align:right">{_fmt_yi(s.turnover)}</td>'
        f'</tr>'
        for s in stocks
    )
    return f'''<div class="card"><h2>{_esc(title)}</h2>
      <table>
        <thead><tr>
          <th>代號</th><th>名稱</th><th>產業</th>
          <th style="text-align:right">漲跌幅</th>
          <th style="text-align:right">收盤</th>
          <th style="text-align:right">成交金額</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>'''


def _render_stocks_row(data: HtmlReportData) -> str:
    return f'''<div class="row">
      {_render_stocks_table(data.digest.top_gainers, "熱門個股 · 漲幅 Top")}
      {_render_stocks_table(data.digest.top_losers, "弱勢個股 · 跌幅 Top")}
    </div>'''


def _render_turnover(data: HtmlReportData) -> str:
    stocks = data.digest.top_turnover
    if not stocks:
        return '<div class="card"><h2>成交活躍 Top</h2><div class="stat-line">（資料不足）</div></div>'
    max_money = max(s.turnover for s in stocks) or 1
    rows = []
    for i, s in enumerate(stocks, 1):
        width = int(s.turnover / max_money * 100)
        color = _COLOR_UP if s.pct > 0 else (_COLOR_DOWN if s.pct < 0 else _COLOR_FLAT)
        rows.append(
            f'<div class="turnover-row">'
            f'<div class="rank">#{i}</div>'
            f'<div class="name">{_esc(s.stock_id)} {_esc(s.name)} <span class="chip">{_esc(s.industry)}</span></div>'
            f'<div class="bar"><span style="width:{width}%;background:{color}"></span></div>'
            f'<div style="text-align:right;white-space:nowrap;font-size:12px;">{_fmt_yi(s.turnover)}元 · 收 {s.close:,.2f} · <span class="{_pct_class(s.pct)}">{_fmt_pct(s.pct)}</span></div>'
            f'</div>'
        )
    return f'''<div class="card"><h2>成交活躍 Top {len(stocks)}</h2>
      {"".join(rows)}
    </div>'''


def _render_extra_sections(data: HtmlReportData) -> str:
    if not data.extra_sections:
        return ""
    blocks = []
    for sec in data.extra_sections:
        t = _esc(sec.get("title", ""))
        body = sec.get("body_html") or _esc(sec.get("body", ""))
        blocks.append(f'<div class="card"><h2>{t}</h2>{body}</div>')
    return "".join(blocks)


def render_html_report(data: HtmlReportData) -> str:
    img_html = ""
    if data.image_url:
        img_html = f'<img class="cover-img" src="{_esc(data.image_url)}" alt="dashboard">'
    footer = _esc(data.footer or f"Generated at {data.generated_at}")
    parts = [
        "<!doctype html>",
        '<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(data.title)}</title>",
        f"<style>{_CSS}</style>",
        "</head><body>",
        '<div class="container">',
        _render_header(data),
        img_html,
        _render_index_card(data),
        _render_themes_cards(data),
        _render_stocks_row(data),
        _render_turnover(data),
        _render_extra_sections(data),
        f'<div class="footer">{footer}</div>',
        "</div></body></html>",
    ]
    return "".join(parts)


def write_html_report(data: HtmlReportData, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html_report(data), encoding="utf-8")
    logger.info("HTML 儀表板已寫入 %s", out_path)
    return out_path
