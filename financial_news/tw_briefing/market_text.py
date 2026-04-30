"""大盤敘述、影線、漲跌幅等純文字工具。"""
from __future__ import annotations

from typing import List, Optional

from financial_news.tw_briefing.finmind_client import IndexBar


def pct_change(prev_close: float, close: float) -> float:
    if prev_close == 0:
        return 0.0
    return (close - prev_close) / prev_close * 100.0


def describe_index_session(bar: IndexBar, prev_close: Optional[float]) -> str:
    """單一交易日加權指數敘述。"""
    rng = bar.high - bar.low
    eps = max(rng * 0.02, 1.0) if rng > 0 else 1.0
    body_hi = max(bar.open, bar.close)
    body_lo = min(bar.open, bar.close)
    upper = bar.high > body_hi + eps
    lower = bar.low < body_lo - eps
    shadow_bits = []
    if upper:
        shadow_bits.append("上影線偏長")
    if lower:
        shadow_bits.append("下影線偏長")
    shadow_txt = f"（{'、'.join(shadow_bits)}）" if shadow_bits else ""

    chg = ""
    if prev_close and prev_close > 0:
        diff = bar.close - prev_close
        p = pct_change(prev_close, bar.close)
        sign = "+" if p >= 0 else ""
        chg = (
            f"漲跌 {sign}{diff:.2f} 點（{sign}{p:.2f}%）"
            f"（昨收 {prev_close:.2f} → 收 {bar.close:.2f}）"
        )
    else:
        chg = f"收 {bar.close:.2f} 點"

    vol = bar.trading_money
    vol_txt = f"成交金額約 {vol / 1e8:.2f} 億元" if vol else "成交金額（資料缺）"

    return (
        f"開 {bar.open:.2f} / 高 {bar.high:.2f} / 低 {bar.low:.2f} / 收 {bar.close:.2f}，"
        f"{chg}，{vol_txt}{shadow_txt}"
    )


def bars_daily_return_pct(bars: List[IndexBar]) -> Optional[float]:
    if len(bars) < 2:
        return None
    a, b = bars[-2], bars[-1]
    if a.close == 0:
        return None
    return pct_change(a.close, b.close)
