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
    weighted_urls: list[tuple[int, int, str]] = []
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    for idx, (raw_url, meta) in enumerate(data.items()):
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
            priority = "medium"
            if isinstance(meta, dict):
                priority = str(meta.get("priority", "medium")).strip().lower()
            weighted_urls.append((priority_rank.get(priority, 1), idx, url))
    weighted_urls.sort(key=lambda x: (x[0], x[1]))
    return [u for _, _, u in weighted_urls]


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

        # 台股盤前／盤後簡報（FinMind + TWSE／FinMind 公告）
        self.finmind_token = os.getenv("FINMIND_TOKEN", "").strip()
        self.tw_briefing_enabled = _bool_env("TW_BRIEFING_ENABLED", False)
        _state = os.getenv("TW_STATE_DIR", "data/briefing_state").strip() or "data/briefing_state"
        self.tw_state_dir = Path(_state)
        if not self.tw_state_dir.is_absolute():
            self.tw_state_dir = _REPO_ROOT / self.tw_state_dir

        self.tw_top_turnover_n = int(os.getenv("TW_TOP_TURNOVER_N", "12"))
        if self.tw_top_turnover_n < 1:
            self.tw_top_turnover_n = 1
        if self.tw_top_turnover_n > 50:
            self.tw_top_turnover_n = 50

        self.tw_top_sector_k = int(os.getenv("TW_TOP_SECTOR_K", "3"))
        if self.tw_top_sector_k < 1:
            self.tw_top_sector_k = 1
        if self.tw_top_sector_k > 10:
            self.tw_top_sector_k = 10

        self.tw_sector_leaders_per_industry = int(
            os.getenv("TW_SECTOR_LEADERS_PER_INDUSTRY", "3")
        )
        if self.tw_sector_leaders_per_industry < 1:
            self.tw_sector_leaders_per_industry = 1
        if self.tw_sector_leaders_per_industry > 10:
            self.tw_sector_leaders_per_industry = 10

        # DEPRECATED: 由 TW_EVENTS_LOOKAHEAD_DAYS（滾動視窗）取代；保留欄位避免讀取錯誤，
        # 但 premarket 已不再使用（永遠採用滾動視窗）
        self.tw_weekly_briefing_only_monday = _bool_env("TW_WEEKLY_BRIEFING_ONLY_MONDAY", False)
        # FinMind TaiwanStockPrice：加權指數請用 TAIEX（IX0001 常回空）
        self.tw_index_stock_id = os.getenv("TW_INDEX_STOCK_ID", "TAIEX").strip() or "TAIEX"
        # proxy 觀察池：聚焦市值權值股 + 當前熱門族群。
        #
        # 設計原則（與 2026-05 user 確認）：
        # 1. 市值前 10 大：抓大盤主軸與資金流向
        # 2. 被動元件（user 指定完整名單，含中小型）
        # 3. 低軌衛星、ABF、光通、面板三虎：user 指定股號或要求「完整」
        # 4. 其他熱門族群（功率元件 / AI 伺服器 / 記憶體 / 半導體設備 /
        #    廠務無塵室 / 散熱）：每族群最多 3 檔代表性個股
        _default_proxies = (
            # ---- 市值前 10 大（權值股，2026 年參考排序）-------------------
            # 2330 台積電 / 2317 鴻海 / 2454 聯發科 / 2382 廣達 /
            # 2891 中信金 / 2881 富邦金 / 2412 中華電 / 2308 台達電 /
            # 3008 大立光 / 2882 國泰金
            "2330,2317,2454,2382,2891,2881,2412,2308,3008,2882,"
            # ---- 被動元件（user 指定完整名單，含圖片二）------------------
            # 國巨 / 華新科 / 立隆電 / 蜜望實 / 信昌電陶 / 禾伸堂 /
            # 鈺邦科技 / 臺慶科 / 日電貿 / 三集瑞-KY / 勤凱科技
            "2327,2492,2472,8043,6173,3026,6449,3357,3090,6862,4760,"
            # ---- 功率元件（最多 3 檔）-----------------------------------
            # 6573 大中 / 8261 富鼎 / 3105 穩懋
            "6573,8261,3105,"
            # ---- 硬體老 AI 伺服器（最多 3，2317/2382 已在權值，故僅補 3231）
            # 3231 緯創 / 6669 緯穎 / 2356 英業達
            "3231,6669,2356,"
            # ---- 記憶體 ------------------------------------
            # 2344 華邦電 / 2408 南亞科 / 2337 旺宏 / 8299 群聯
            "2344,2408,2337,8299,"
            # ---- 半導體設備（user 圖一完整名單）-----------------------
            # 3583 辛耘 / 3131 弘塑 / 2467 志聖 / 6640 均華 /
            # 6187 萬潤 / 3563 牧德 / 5443 均豪
            "3583,3131,2467,6640,6187,3563,5443,"
            # ---- 廠務股及無塵室（user 圖二完整名單）--------------------
            # 2404 漢唐 / 6139 亞翔 / 4766 帆宣 / 6691 洋基 /
            # 5536 聖暉 / 6667 信紘科
            "2404,6139,4766,6691,5536,6667,"
            # ---- 散熱（最多 3 檔）-------------------------------------
            # 3017 奇鋐 / 3324 雙鴻 / 3653 健策
            "3017,3324,3653,"
            # ---- 低軌衛星（user 指定）---------------------------------
            # 3491 昇達科 / 2313 華通
            "3491,2313,"
            # ---- ABF 載板（user 指定 4 檔）----------------------------
            # 3037 欣興 / 8046 南電 / 3189 景碩 / 2368 金像電
            "3037,8046,3189,2368,"
            # ---- 光通族群（user 要求完整）-----------------------------
            # 3450 聯鈞 / 4979 華星光 / 4977 眾達-KY / 8086 宏捷科 /
            # 3163 波若威 / 3081 聯亞
            "3450,4979,4977,8086,3163,3081,"
            # ---- 面板三虎 -----------------------------------------------
            # 2409 友達 / 3481 群創 / 6116 彩晶
            "2409,3481,6116,"
            # ---- 航運龍頭（3 家）----------------------------------------
            # 2603 長榮 / 2609 陽明 / 2615 萬海
            "2603,2609,2615,"
            # ---- CCL 銅箔基板（user 指定）--------------------------------
            # 2383 台光電 / 6213 聯茂 / 6274 台燿
            "2383,6213,6274"
        )
        _proxies = os.getenv("TW_MARKET_PROXY_STOCKS", _default_proxies)
        self.tw_market_proxy_stocks = [s.strip() for s in _proxies.split(",") if s.strip()]
        self.tw_non_trading_notify = _bool_env("TW_NON_TRADING_NOTIFY", False)
        self.tw_weekly_events_max_per_day = int(os.getenv("TW_WEEKLY_EVENTS_MAX_PER_DAY", "5"))
        if self.tw_weekly_events_max_per_day < 1:
            self.tw_weekly_events_max_per_day = 1
        if self.tw_weekly_events_max_per_day > 20:
            self.tw_weekly_events_max_per_day = 20

        # 滾動視窗：今日 + 未來 N 個交易日的事件預告
        self.tw_events_lookahead_days = int(os.getenv("TW_EVENTS_LOOKAHEAD_DAYS", "5"))
        if self.tw_events_lookahead_days < 0:
            self.tw_events_lookahead_days = 0
        if self.tw_events_lookahead_days > 20:
            self.tw_events_lookahead_days = 20

        # 預告涵蓋哪些事件 kind（逗號分隔）；預設核心三項
        # 可選：exdiv,shareholder_meeting,short_cover,suspended_resume
        # 註：shareholder_meeting / short_cover / book_close 會以 watch_tickers 過濾，
        # 僅保留觀察清單內個股（proxy + 今日除權息／注意／處置）
        _kinds = os.getenv(
            "TW_EVENTS_LOOKAHEAD_KINDS",
            "exdiv,shareholder_meeting,short_cover",
        )
        self.tw_events_lookahead_kinds = [
            k.strip() for k in _kinds.split(",") if k.strip()
        ]

        # 進行中區塊涵蓋哪些 kind（逗號分隔）
        # 可選：disposal,book_close（book_close 同樣會以 watch_tickers 過濾）
        _ip = os.getenv("TW_EVENTS_INPROGRESS_KINDS", "disposal,book_close")
        self.tw_events_inprogress_kinds = [
            k.strip() for k in _ip.split(",") if k.strip()
        ]

        # 盤後是否附「明日預告」精簡列
        self.tw_postmarket_show_next_day = _bool_env("TW_POSTMARKET_SHOW_NEXT_DAY", True)

        # 視覺化與推送
        self.imgbb_api_key = os.getenv("IMGBB_API_KEY", "").strip()
        _report_dir = os.getenv("TW_REPORT_DIR", "data/briefing_report").strip() or "data/briefing_report"
        self.tw_report_dir = Path(_report_dir)
        if not self.tw_report_dir.is_absolute():
            self.tw_report_dir = _REPO_ROOT / self.tw_report_dir

        # push_mode: visual（預設）→ 圖片 + Flex 卡；若上傳失敗自動降級為 text
        #           text           → 仍走舊的純文字摘要
        mode = os.getenv("TW_PUSH_MODE", "visual").strip().lower()
        self.tw_push_mode = mode if mode in ("visual", "text") else "visual"

        self.tw_hot_themes_k = int(os.getenv("TW_HOT_THEMES_K", "4"))
        if self.tw_hot_themes_k < 1:
            self.tw_hot_themes_k = 1
        if self.tw_hot_themes_k > 10:
            self.tw_hot_themes_k = 10

        self.tw_hot_gainers_n = int(os.getenv("TW_HOT_GAINERS_N", "8"))
        if self.tw_hot_gainers_n < 1:
            self.tw_hot_gainers_n = 1
        if self.tw_hot_gainers_n > 30:
            self.tw_hot_gainers_n = 30

        self.tw_hot_losers_n = int(os.getenv("TW_HOT_LOSERS_N", "5"))
        if self.tw_hot_losers_n < 1:
            self.tw_hot_losers_n = 1
        if self.tw_hot_losers_n > 30:
            self.tw_hot_losers_n = 30

        # 盤後排程：FinMind TaiwanStockPrice 官方標示「星期一至五 17:30 更新」，
        # 實測收盤後約 14:00～14:30 多半已有當日 K 線；故預設 14:30 + 開啟重試以防延遲。
        # 可由 .env 覆寫。
        self.tw_postmarket_hour = int(os.getenv("TW_POSTMARKET_HOUR", "14"))
        if not 0 <= self.tw_postmarket_hour <= 23:
            self.tw_postmarket_hour = 14
        self.tw_postmarket_minute = int(os.getenv("TW_POSTMARKET_MINUTE", "30"))
        if not 0 <= self.tw_postmarket_minute <= 59:
            self.tw_postmarket_minute = 30

        # 嚴格驗日：取到的 idx_bars 最後一筆若不是 ref（today），是否視為「未公布」
        # 預設 True（FinMind 延遲時不要產生穿著今日衣服的舊資料報告）
        self.tw_postmarket_strict_today = _bool_env("TW_POSTMARKET_STRICT_TODAY", True)

        # 拿不到當日資料時的重試次數與間隔（分鐘）
        self.tw_postmarket_max_retries = int(os.getenv("TW_POSTMARKET_MAX_RETRIES", "3"))
        if self.tw_postmarket_max_retries < 0:
            self.tw_postmarket_max_retries = 0
        if self.tw_postmarket_max_retries > 12:
            self.tw_postmarket_max_retries = 12
        self.tw_postmarket_retry_interval_min = int(
            os.getenv("TW_POSTMARKET_RETRY_INTERVAL_MIN", "10")
        )
        if self.tw_postmarket_retry_interval_min < 1:
            self.tw_postmarket_retry_interval_min = 1
        if self.tw_postmarket_retry_interval_min > 60:
            self.tw_postmarket_retry_interval_min = 60

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
