"""LINE Messaging API push（精簡版，僅文字）。"""
from __future__ import annotations

from typing import List

import requests

from financial_news.utils import setup_logger

logger = setup_logger(__name__)

PUSH_MESSAGE_URL = "https://api.line.me/v2/bot/message/push"

# 單則文字訊息上限約 5000，保留緩衝
MAX_TEXT_LEN = 4500


class LineNotifier:
    def __init__(self, channel_access_token: str, user_id: str) -> None:
        self._token = channel_access_token
        self._user_id = user_id

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
            if not self._push_messages([{"type": "text", "text": chunk}]):
                ok = False
        return ok

    def _push_messages(self, messages: List[dict]) -> bool:
        try:
            resp = requests.post(
                PUSH_MESSAGE_URL,
                headers=self._headers(),
                json={"to": self._user_id, "messages": messages},
                timeout=30,
            )
            if resp.status_code == 200:
                logger.info("LINE push 成功（%d 則訊息區塊）", len(messages))
                return True
            logger.error("LINE push 失敗: %s %s", resp.status_code, resp.text)
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
