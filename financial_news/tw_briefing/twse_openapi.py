"""TWSE OpenAPI 共用 fetcher。

收斂 ``twse_announcements`` / ``twse_market`` / ``exdividend`` 三處重複的
``requests.get(...) + raise_for_status + json + try/except`` 模式。

策略：失敗 log + 回 ``[]``（與既有三處一致；因為 TWSE 偶有 500/503，
不該因此讓整份盤前/盤後簡報崩掉）。
"""
from __future__ import annotations

from typing import Any, Dict, List

import requests

from financial_news.core.http import HttpClient
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)

# 模組層共用 client：所有 TWSE OpenAPI 呼叫共享 Session（連線池/keep-alive）
_client = HttpClient(timeout=60.0, name="twse")


def fetch_twse_list(url: str) -> List[Dict[str, Any]]:
    """GET TWSE OpenAPI list endpoint；任何錯誤 log 後回 ``[]``。

    TWSE OpenAPI 全部回 JSON list；若 endpoint 暫不可用（500）或回傳格式
    異常，呼叫端通常希望「降級為空」而非崩潰。
    """
    try:
        data = _client.get_json(url)
    except (requests.RequestException, ValueError) as e:
        logger.warning("TWSE OpenAPI 擷取失敗（%s）：%s", url, e)
        return []
    if not isinstance(data, list):
        logger.warning("TWSE OpenAPI 回傳非 list（%s）：%r", url, type(data))
        return []
    return data
