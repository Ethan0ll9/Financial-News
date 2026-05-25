"""Telegram Bot API 推播：文字、圖片。

官方文件：https://core.telegram.org/bots/api

與 LINE 差異：
- 無 Flex Message；盤前／盤後改以 sendPhoto（儀表板）+ sendMessage（文字）送達
- 單則文字上限 4096 字元（LINE 約 4500）
- 需 Bot Token + Chat ID（私聊、群組或頻道）
"""
from __future__ import annotations

from typing import List, Optional

import requests

from financial_news.core.api_endpoints import telegram_api_url
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)

MAX_TEXT_LEN = 4096
_CAPTION_MAX = 1024


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token = (bot_token or "").strip()
        self._chat_id = (chat_id or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def push_text_chunks(self, text: str) -> bool:
        if not self.configured:
            logger.debug("Telegram 未設定，略過文字推播")
            return False
        if not text or not text.strip():
            return True
        ok = True
        for chunk in _split_text(text, MAX_TEXT_LEN):
            if not self._send_message(chunk):
                ok = False
        return ok

    def push_photo(self, photo_url: str, *, caption: Optional[str] = None) -> bool:
        if not self.configured:
            return False
        if not photo_url:
            return False
        cap = (caption or "").strip()
        if len(cap) > _CAPTION_MAX:
            cap = cap[: _CAPTION_MAX - 3] + "..."
        payload: dict = {
            "chat_id": self._chat_id,
            "photo": photo_url,
        }
        if cap:
            payload["caption"] = cap
        return self._post("sendPhoto", payload)

    def push_messages(self, messages: List[dict]) -> bool:
        """盡力對應 LINE 訊息格式：text / image / flex（flex 取 altText + hero 圖）。"""
        if not self.configured:
            return False
        if not messages:
            return True
        ok = True
        for msg in messages:
            mtype = msg.get("type")
            if mtype == "text":
                if not self.push_text_chunks(str(msg.get("text") or "")):
                    ok = False
            elif mtype == "image":
                url = msg.get("originalContentUrl") or msg.get("previewImageUrl")
                if url and not self.push_photo(str(url)):
                    ok = False
            elif mtype == "flex":
                alt = str(msg.get("altText") or "台股簡報")
                hero_url = _flex_hero_url(msg)
                if hero_url:
                    if not self.push_photo(hero_url, caption=alt):
                        ok = False
                elif not self.push_text_chunks(alt):
                    ok = False
            else:
                logger.debug("Telegram 略過不支援的訊息類型: %s", mtype)
        return ok

    def _send_message(self, text: str) -> bool:
        return self._post(
            "sendMessage",
            {"chat_id": self._chat_id, "text": text},
        )

    def _post(self, method: str, payload: dict) -> bool:
        url = telegram_api_url(self._token, method)
        try:
            resp = requests.post(url, json=payload, timeout=30)
        except requests.RequestException as e:
            logger.error("Telegram %s 例外: %s", method, e)
            return False
        if resp.status_code == 200:
            body = resp.json()
            if body.get("ok"):
                logger.info("Telegram %s 成功", method)
                return True
            logger.error("Telegram %s ok=false: %s", method, body)
            return False
        logger.error("Telegram %s 失敗: %s %s", method, resp.status_code, resp.text[:300])
        return False


def _flex_hero_url(msg: dict) -> Optional[str]:
    contents = msg.get("contents")
    if not isinstance(contents, dict):
        return None
    hero = contents.get("hero")
    if isinstance(hero, dict) and hero.get("type") == "image":
        url = hero.get("url")
        return str(url) if url else None
    return None


def _split_text(text: str, max_len: int) -> List[str]:
    if len(text) <= max_len:
        return [text]
    parts: List[str] = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            parts.append(rest)
            break
        cut = rest.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    return parts
