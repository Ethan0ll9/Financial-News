"""LINE Messaging API push：文字、圖片、Flex。"""
from __future__ import annotations

from typing import List, Optional

import requests

from financial_news.core.api_endpoints import LINE_PUSH_MESSAGE_URL as PUSH_MESSAGE_URL
from financial_news.core.http import HttpClient
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)

MAX_TEXT_LEN = 4500
_MAX_MESSAGES_PER_PUSH = 5


class LineNotifier:
    def __init__(self, channel_access_token: str, user_id: str) -> None:
        self._token = channel_access_token
        self._user_id = user_id
        # 共享 Session：所有 push 重用 keep-alive 連線；timeout 沿用原本 30s
        self._http = HttpClient(timeout=30.0, name="line")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def push_text_chunks(self, text: str) -> bool:
        """將過長文字拆成多則依序 push。"""
        if not self._token or not self._user_id:
            logger.error("LINE 設定不完整，略過發送")
            return False
        chunks = _split_text(text, MAX_TEXT_LEN)
        ok = True
        for chunk in chunks:
            if not self._push([{"type": "text", "text": chunk}]):
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
        return self._push([msg])

    def push_flex(self, contents: dict, alt_text: str) -> bool:
        msg = {
            "type": "flex",
            "altText": alt_text[:400] if alt_text else "台股簡報",
            "contents": contents,
        }
        return self._push([msg])

    def push_messages(self, messages: List[dict]) -> bool:
        """可混合多種訊息（text/image/flex）合併送出；>5 則會分批。"""
        if not messages:
            return True
        if not self._token or not self._user_id:
            logger.error("LINE 設定不完整，略過發送")
            return False
        ok = True
        for i in range(0, len(messages), _MAX_MESSAGES_PER_PUSH):
            chunk = messages[i : i + _MAX_MESSAGES_PER_PUSH]
            if not self._push(chunk):
                ok = False
        return ok

    def _push(self, messages: List[dict]) -> bool:
        if not self._token or not self._user_id:
            logger.error("LINE 設定不完整，略過發送")
            return False
        try:
            resp = self._http.post_json(
                PUSH_MESSAGE_URL,
                json={"to": self._user_id, "messages": messages},
                headers=self._headers(),
            )
            if resp.status_code == 200:
                logger.info("LINE push 成功（%d 則訊息區塊）", len(messages))
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
