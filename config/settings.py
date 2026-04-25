"""從環境變數載入設定。"""
import os

from dotenv import load_dotenv

load_dotenv()


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

        self.cnyes_enabled = _bool_env("CNYES_ENABLED", True)
        self.cnyes_category = os.getenv("CNYES_CATEGORY", "all").strip() or "all"

        self.rss_enabled = _bool_env("RSS_ENABLED", False)
        rss_urls = os.getenv("RSS_FEED_URLS", "")
        self.rss_feed_urls = [
            u.strip() for u in rss_urls.split(",") if u.strip()
        ]

        self.run_on_start = _bool_env("RUN_ON_START", False)

        self._validate()

    def _validate(self) -> None:
        if self.rss_enabled and not self.rss_feed_urls:
            import warnings

            warnings.warn(
                "RSS_ENABLED 為 true 但 RSS_FEED_URLS 為空，digest 將略過 RSS。",
                stacklevel=2,
            )


settings = Settings()
