"""RSS URL → 國家／地區、媒體名稱（對照 config/rss_feed_catalog.json）。"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

_DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "config" / "rss_feed_catalog.json"


def _catalog_path() -> Path:
    configured = os.getenv("RSS_FEEDS_FILE", "").strip()
    if not configured:
        return _DEFAULT_CATALOG_PATH
    p = Path(configured)
    if p.is_absolute():
        return p
    return Path(__file__).resolve().parents[2] / p


def _feed_label_fallback(url: str) -> str:
    host = urlparse(url).netloc or url
    return f"RSS（{host}）"


@lru_cache(maxsize=1)
def _raw_catalog() -> Dict[str, Any]:
    catalog_path = _catalog_path()
    if not catalog_path.is_file():
        return {}
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def rss_feed_meta(feed_url: str) -> Tuple[Optional[str], Optional[str], str]:
    """回傳 (region, outlet, source_label)。

    若 catalog 無此 URL，region/outlet 為 None，source_label 為簡易 host 標籤。
    """
    url = feed_url.strip()
    cat = _raw_catalog()
    candidates = (
        url,
        url.replace("http://", "https://", 1),
        url.replace("https://", "http://", 1),
    )
    meta = None
    for u in candidates:
        if u in cat:
            meta = cat.get(u)
            break
    if isinstance(meta, dict):
        region = (meta.get("region") or "").strip() or None
        outlet = (meta.get("outlet") or "").strip() or None
        if region and outlet:
            return region, outlet, f"{region} · {outlet}"
    return None, None, _feed_label_fallback(url)
