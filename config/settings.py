"""從環境變數載入設定。"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_CONFIG_DIR = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_RSS_CATALOG = _CONFIG_DIR / "rss_feed_catalog.json"


def _load_rss_urls_from_catalog(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    urls: list[str] = []
    for raw_url, meta in data.items():
        if not isinstance(raw_url, str):
            continue
        url = raw_url.strip()
        if not url:
            continue
        # 預設視為啟用；可在 catalog 設定 enabled=false 臨時停用問題來源
        enabled = True
        if isinstance(meta, dict) and "enabled" in meta:
            enabled = bool(meta.get("enabled"))
        if enabled:
            urls.append(url)
    return urls


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    """應用程式設定。"""

    def __init__(self) -> None:
        self.line_channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        self.line_user_id = os.getenv("LINE_USER_ID", "").strip()

        self.top_n = int(os.getenv("TOP_N", "10"))

        # RSS：每個 feed 最多取幾則（與 TOP_N 分開；TOP_N 仍用於鉅亨等單一來源）
        self.rss_items_per_feed = int(os.getenv("RSS_ITEMS_PER_FEED", "5"))
        if self.rss_items_per_feed < 1:
            self.rss_items_per_feed = 1
        if self.rss_items_per_feed > 50:
            self.rss_items_per_feed = 50

        # RSS：合併全部 feed 後最多保留幾則（選填；例如 30 家×5 則可設 150 控制 LINE 長度）
        self.rss_max_total = None
        _rss_max = os.getenv("RSS_MAX_TOTAL", "").strip()
        if _rss_max:
            try:
                mt = int(_rss_max)
                if mt > 0:
                    self.rss_max_total = min(mt, 2000)
            except ValueError:
                pass

        self.cnyes_enabled = _bool_env("CNYES_ENABLED", True)
        self.cnyes_category = os.getenv("CNYES_CATEGORY", "all").strip() or "all"

        self.rss_enabled = _bool_env("RSS_ENABLED", False)
        rss_urls_env = os.getenv("RSS_FEED_URLS", "")
        env_list = [u.strip() for u in rss_urls_env.split(",") if u.strip()]
        if env_list:
            self.rss_feed_urls = env_list
        else:
            catalog_file = os.getenv("RSS_FEEDS_FILE", "").strip()
            if catalog_file:
                p = Path(catalog_file)
                if not p.is_absolute():
                    p = _REPO_ROOT / p
            else:
                p = _DEFAULT_RSS_CATALOG
            self.rss_feed_urls = _load_rss_urls_from_catalog(p)

        self.run_on_start = _bool_env("RUN_ON_START", False)

        self._validate()

    def _validate(self) -> None:
        if self.rss_enabled and not self.rss_feed_urls:
            import warnings

            warnings.warn(
                "RSS_ENABLED 為 true 但沒有任何 RSS URL（"
                "請設定 RSS_FEED_URLS 或提供 config/rss_feed_catalog.json），digest 將略過 RSS。",
                stacklevel=2,
            )


settings = Settings()
