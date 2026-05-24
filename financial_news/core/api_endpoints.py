"""所有對外 API endpoint URL（單一事實來源）。

不放 token、timeout、headers；那些屬於 client 或 settings 的職責。
更新 endpoint 時只改本檔，避免散落在多個模組導致漏改。
"""
from __future__ import annotations

# ---- FinMind ----------------------------------------------------------------
FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"

# ---- TWSE OpenAPI -----------------------------------------------------------
TWSE_FMTQIK_URL = "https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK"
TWSE_TWT48U_URL = "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"
TWSE_NOTICE_URL = "https://openapi.twse.com.tw/v1/announcement/notice"
TWSE_PUNISH_URL = "https://openapi.twse.com.tw/v1/announcement/punish"
TWSE_SHAREHOLDER_MEETING_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap38_L"

# ---- LINE -------------------------------------------------------------------
LINE_PUSH_MESSAGE_URL = "https://api.line.me/v2/bot/message/push"

# ---- imgbb ------------------------------------------------------------------
IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"

# ---- Cnyes ------------------------------------------------------------------
CNYES_POPULAR_URL = "https://api.cnyes.com/media/api/v1/newslist/popular"
CNYES_ARTICLE_URL_TEMPLATE = "https://news.cnyes.com/news/id/{news_id}"

# ---- Telegram Bot API -------------------------------------------------------
TELEGRAM_API_BASE = "https://api.telegram.org/bot"


def telegram_api_url(bot_token: str, method: str) -> str:
    """組出 ``https://api.telegram.org/bot<token>/<method>``。"""
    return f"{TELEGRAM_API_BASE}{bot_token}/{method}"
