"""日期字串解析共用工具。

收斂多處重複的 ``datetime.strptime(s[:10], "%Y-%m-%d").date()`` 寫法，
集中處理空字串／None／非預期格式時回傳 ``None`` 的策略。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional


def parse_iso_date(s: Optional[str]) -> Optional[date]:
    """將 ``YYYY-MM-DD`` 開頭的字串解析為 ``date``；不合法回 ``None``。

    僅取前 10 個字元（容許後綴時間，例如 ``2026-04-30 08:00:00``）。
    """
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
