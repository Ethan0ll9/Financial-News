"""TWSE OpenAPI 數字欄位解析共用工具。

TWSE 開放資料常以**字串**回傳數字並含千分位逗號（例如 ``"1,248,027,623,513"``），
解析時要先去除逗號才能轉 float／int；空值或非數字字串需安全降級為 0。
"""
from __future__ import annotations

from typing import Any


def parse_twse_float(v: Any) -> float:
    """將 TWSE 字串數字（含千分位逗號）轉為 float；失敗回 0.0。"""
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def parse_twse_int(v: Any) -> int:
    """將 TWSE 字串數字（含千分位逗號／小數）轉為 int；失敗回 0。

    先走 float 再轉 int，可同時處理 ``"1,234"``、``"1,234.0"``、``"1.5e3"``。
    """
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0
