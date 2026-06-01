"""多通道推播：LINE + Telegram（可選）。

對外介面與 ``LineNotifier`` 相同，呼叫端只需 ``NotifyHub(settings)``，
不必在每處重複判斷 Telegram 是否啟用。

LINE 推播策略：
  - LINE_GROUP_ID 或 LINE_USER_ID 有設定 → Push 給指定對象
  - 兩者皆空 → Broadcast 給所有加好友的使用者（預設）
"""
from __future__ import annotations

from typing import List, Optional

from config.settings import Settings
from financial_news.line_notifier import LineNotifier
from financial_news.telegram_notifier import TelegramNotifier


class NotifyHub:
    """同時推送到 LINE；若 ``TELEGRAM_ENABLED`` 且 token/chat_id 齊全則一併推 Telegram。"""

    def __init__(self, settings: Settings) -> None:
        self._line = LineNotifier(
            settings.line_channel_access_token,
            settings.line_recipient_id,  # 空字串 → 自動走 broadcast
        )
        self._telegram: Optional[TelegramNotifier] = None
        if settings.telegram_enabled:
            tg = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
            if tg.configured:
                self._telegram = tg

    def push_text_chunks(self, text: str) -> bool:
        ok_line = self._line.push_text_chunks(text)
        ok_tg = True
        if self._telegram:
            ok_tg = self._telegram.push_text_chunks(text)
        return ok_line and ok_tg

    def push_image(self, image_url: str, preview_url: Optional[str] = None) -> bool:
        ok_line = self._line.push_image(image_url, preview_url=preview_url)
        ok_tg = True
        if self._telegram:
            ok_tg = self._telegram.push_photo(image_url)
        return ok_line and ok_tg

    def push_flex(self, contents: dict, alt_text: str) -> bool:
        ok_line = self._line.push_flex(contents, alt_text)
        ok_tg = True
        if self._telegram:
            # LINE Flex → Telegram：儀表板圖 + 標題當 caption
            hero = contents.get("hero") if isinstance(contents, dict) else None
            url = None
            if isinstance(hero, dict) and hero.get("type") == "image":
                url = hero.get("url")
            if url:
                ok_tg = self._telegram.push_photo(str(url), caption=alt_text)
            else:
                ok_tg = self._telegram.push_text_chunks(alt_text)
        return ok_line and ok_tg

    def push_messages(self, messages: List[dict]) -> bool:
        ok_line = self._line.push_messages(messages)
        ok_tg = True
        if self._telegram:
            ok_tg = self._telegram.push_messages(messages)
        return ok_line and ok_tg
