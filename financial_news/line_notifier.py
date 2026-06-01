"""LINE Messaging API push / broadcast：文字、圖片、Flex。

預設使用 Broadcast（廣播）將訊息發送給所有加好友的使用者，不需要指定 recipient_id。
若有設定 recipient_id（userId U... 或 groupId C...），則改用 Push API 推送給特定對象。
"""
from __future__ import annotations

from typing import List, Optional

import requests

from financial_news.core.api_endpoints import (
    LINE_BROADCAST_URL,
    LINE_PUSH_MESSAGE_URL as PUSH_MESSAGE_URL,
)
from financial_news.core.http import HttpClient
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)

MAX_TEXT_LEN = 4500
_MAX_MESSAGES_PER_PUSH = 5


class LineNotifier:
    def __init__(self, channel_access_token: str, recipient_id: str = "") -> None:
        self._token = channel_access_token
        self._recipient_id = recipient_id
        self._http = HttpClient(timeout=30.0, name="line")

    @property
    def _use_broadcast(self) -> bool:
        """無 recipient_id 時使用 Broadcast，發給所有加好友的使用者。"""
        return not self._recipient_id

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def push_text_chunks(self, text: str) -> bool:
        """將過長文字拆成多則依序發送。"""
        if not self._token:
            logger.error("LINE 設定不完整，略過發送")
            return False
        chunks = _split_text(text, MAX_TEXT_LEN)
        ok = True
        for chunk in chunks:
            if not self._send([{"type": "text", "text": chunk}]):
                ok = False
        return ok

    def push_image(self, image_url: str, preview_url: Optional[str] = None) -> bool:
        if not image_url:
            return False
        msg = {
            "type": "image",
            "originalContentUrl": image_url,
            "previewImageUrl": preview_url or image_url,
        }
        return self._send([msg])

    def push_flex(self, contents: dict, alt_text: str) -> bool:
        msg = {
            "type": "flex",
            "altText": alt_text[:400] if alt_text else "台股簡報",
            "contents": contents,
        }
        return self._send([msg])

    def push_messages(self, messages: List[dict]) -> bool:
        """可混合多種訊息（text/image/flex）合併送出；>5 則會分批。"""
        if not messages:
            return True
        if not self._token:
            logger.error("LINE 設定不完整，略過發送")
            return False
        ok = True
        for i in range(0, len(messages), _MAX_MESSAGES_PER_PUSH):
            chunk = messages[i : i + _MAX_MESSAGES_PER_PUSH]
            if not self._send(chunk):
                ok = False
        return ok

    def _send(self, messages: List[dict]) -> bool:
        """自動選擇 Broadcast 或 Push。"""
        if not self._token:
            logger.error("LINE 設定不完整，略過發送")
            return False
        if self._use_broadcast:
            return self._broadcast(messages)
        return self._push(messages)

    def _broadcast(self, messages: List[dict]) -> bool:
        try:
            resp = self._http.post_json(
                LINE_BROADCAST_URL,
                json={"messages": messages},
                headers=self._headers(),
            )
            if resp.status_code == 200:
                logger.info("LINE broadcast 成功（%d 則訊息區塊，發給所有好友）", len(messages))
                return True
            logger.error("LINE broadcast 失敗: %s %s", resp.status_code, resp.text[:300])
            return False
        except requests.RequestException as e:
            logger.error("LINE broadcast 例外: %s", e)
            return False

    def _push(self, messages: List[dict]) -> bool:
        try:
            resp = self._http.post_json(
                PUSH_MESSAGE_URL,
                json={"to": self._recipient_id, "messages": messages},
                headers=self._headers(),
            )
            if resp.status_code == 200:
                target_type = "群組" if self._recipient_id.startswith("C") else "個人"
                logger.info("LINE push 成功 → %s（%s，%d 則訊息區塊）", self._recipient_id, target_type, len(messages))
                return True
            logger.error("LINE push 失敗: %s %s", resp.status_code, resp.text[:300])
            return False
        except requests.RequestException as e:
            logger.error("LINE push 例外: %s", e)
            return False


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
