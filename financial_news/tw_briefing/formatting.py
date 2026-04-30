"""共用格式化工具：百分比 / 金額 / 顏色 / emoji 處理。

收斂多處重複的私有 helper（_fmt_pct / _fmt_yi / _pct_color / _color_by_pct /
_pct_class / _strip_emoji），讓 chart_builder、flex_builder、html_report、
market_queries 共用同一份實作，避免格式不一致與重複維護。
"""
from __future__ import annotations

import re

# ---- 顏色常數（紅漲綠跌；台股慣例）-----------------------------------------
COLOR_UP = "#d9534f"
COLOR_DOWN = "#2d8f5a"
COLOR_FLAT = "#95a5a6"

# 漲跌幅判定門檻（百分比）；±0.05% 內視為平盤
_PCT_FLAT_THRESHOLD = 0.05


# ---- 百分比格式 -------------------------------------------------------------


def fmt_pct(p: float) -> str:
    """格式化百分比為 ``+x.xx%`` / ``-x.xx%``，零值含 ``+``。"""
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}%"


# ---- 金額（億元）------------------------------------------------------------


def fmt_yi(value_yuan: float, *, decimals: int = 2) -> str:
    """金額（元）→「X.XX 億」字串。

    ``decimals``：小數位（chart/flex 預設 1，html/market_queries 預設 2，
    需要時呼叫端可自訂）。
    """
    fmt = f"{{:,.{decimals}f}} 億"
    return fmt.format(value_yuan / 1e8)


# ---- 漲跌色／class 映射 -----------------------------------------------------


def pct_color_hex(p: float) -> str:
    """依漲跌幅回傳 hex 顏色（給 chart_builder / flex_builder 用）。"""
    if p > _PCT_FLAT_THRESHOLD:
        return COLOR_UP
    if p < -_PCT_FLAT_THRESHOLD:
        return COLOR_DOWN
    return COLOR_FLAT


def pct_class_name(p: float) -> str:
    """依漲跌幅回傳 CSS class 名稱（給 html_report 用）。"""
    if p > _PCT_FLAT_THRESHOLD:
        return "up"
    if p < -_PCT_FLAT_THRESHOLD:
        return "down"
    return "flat"


# ---- emoji 過濾（避免 matplotlib 缺字警告）---------------------------------

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F2FF"
    "]",
    flags=re.UNICODE,
)


def strip_emoji(s: str) -> str:
    """移除字串中的 emoji 字元並 strip 兩端空白。"""
    return _EMOJI_RE.sub("", s).strip()
