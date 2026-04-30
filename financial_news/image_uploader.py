"""圖床上傳：目前支援 imgbb。未上傳成功則回傳 None。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import requests

from financial_news.core.api_endpoints import IMGBB_UPLOAD_URL as _IMGBB_ENDPOINT
from financial_news.core.http import HttpClient
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)

_IMGBB_MAX_MB = 32


class ImageUploader:
    """包裝 imgbb upload；未提供 api_key 時 ``upload`` 會直接回 None。"""

    def __init__(self, imgbb_api_key: str) -> None:
        self.imgbb_api_key = (imgbb_api_key or "").strip()
        # 共享 Session：每天 1~2 次 upload，仍走 keep-alive；timeout 沿用 60s
        self._http = HttpClient(timeout=60.0, name="imgbb")
        if not self.imgbb_api_key:
            logger.info("IMGBB_API_KEY 未設定，上傳功能停用（僅保留本機檔案）")

    def upload(self, image_path: Path, *, mime: str = "image/png") -> Optional[str]:
        if not self.imgbb_api_key:
            return None
        p = Path(image_path)
        if not p.is_file():
            logger.error("圖片不存在：%s", p)
            return None

        try:
            data_bytes = p.read_bytes()
        except OSError as e:
            logger.error("讀取圖片失敗：%s %s", p, e)
            return None

        size_mb = len(data_bytes) / (1024 * 1024)
        if size_mb > _IMGBB_MAX_MB:
            logger.error("圖片 %.2f MB 超過 imgbb 上限 %d MB", size_mb, _IMGBB_MAX_MB)
            return None

        try:
            resp = self._http.post_multipart(
                _IMGBB_ENDPOINT,
                files={"image": (p.name, data_bytes, mime)},
                data={"key": self.imgbb_api_key},
            )
        except requests.RequestException as e:
            logger.error("imgbb 上傳例外：%s", e)
            return None

        if resp.status_code != 200:
            logger.error("imgbb 上傳失敗：%s %s", resp.status_code, resp.text[:200])
            return None

        try:
            payload = resp.json()
        except ValueError:
            logger.error("imgbb 回應非 JSON：%s", resp.text[:200])
            return None

        if not payload.get("success"):
            logger.error("imgbb 回應 success=false：%s", payload)
            return None

        data = payload.get("data") or {}
        url = data.get("url") or (data.get("image") or {}).get("url")
        if not url:
            logger.error("imgbb 回應缺 url：%s", payload)
            return None
        logger.info("imgbb 上傳成功 (%.2f MB)：%s", size_mb, url)
        return url
