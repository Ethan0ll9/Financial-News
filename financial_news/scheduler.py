"""定時擷取新聞並推播 LINE。"""
from __future__ import annotations

from datetime import datetime
from typing import List

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import settings
from financial_news.notify_hub import NotifyHub
from financial_news.models import NewsItem
from financial_news.sources.base import NewsSource
from financial_news.sources.cnyes import CnyesPopularSource
from financial_news.sources.rss_feed import RssFeedSource
from financial_news.tw_briefing.digest_context import reset_digest_rss_items, set_digest_rss_items
from financial_news.tw_briefing.premarket_report import run_premarket
from financial_news.tw_briefing.postmarket_report import run_postmarket
from financial_news.core.utils import setup_logger, strip_html

logger = setup_logger(__name__)


def _priority_tag(priority: str | None) -> str:
    if not priority:
        return ""
    p = priority.strip().lower()
    if p == "high":
        return "[H] "
    if p == "medium":
        return "[M] "
    if p == "low":
        return "[L] "
    return ""


def _is_line_eligible(item: NewsItem) -> bool:
    """LINE digest 精簡規則：只保留高重要性或台灣媒體。

    LINE Messaging API 免費額度每月 200 則，先前每次 digest 一次推送多區域所有
    來源會很快耗盡，因此這裡聚焦保留：
    - ``priority == "high"`` 的國際與本地重要報導
    - ``region`` 含「台灣」的所有來源（含鉅亨網 popular）
    """
    priority = (item.priority or "").strip().lower()
    region = item.region or ""
    return priority == "high" or "台灣" in region


def build_sources() -> List[NewsSource]:
    sources: List[NewsSource] = []
    if settings.cnyes_enabled:
        sources.append(CnyesPopularSource(category=settings.cnyes_category))
    if settings.rss_enabled and settings.rss_feed_urls:
        sources.append(
            RssFeedSource(
                settings.rss_feed_urls,
                items_per_feed=settings.rss_items_per_feed,
                max_total_items=settings.rss_max_total,
            )
        )
    return sources


def _format_block(source: NewsSource, items: List[NewsItem], ts: str) -> str:
    lines = [
        f"【{source.name}】",
        f"擷取時間：{ts}",
        "",
    ]
    if not items:
        lines.append("（本輪無可用項目）")
        return "\n".join(lines)
    for i, it in enumerate(items, 1):
        ptag = _priority_tag(it.priority)
        if it.region and it.outlet:
            head = f"{i}. {ptag}[{it.region}｜{it.outlet}]"
        else:
            head = f"{i}. {ptag}[{it.source_label}]"
        lines.append(head)
        lines.append(f"   {strip_html(it.title)}")
        lines.append(f"   {it.url}")
        lines.append("")
    return "\n".join(lines).rstrip()


def run_digest() -> None:
    """擷取所有啟用來源，合併為一則訊息推播（節省 LINE 月配額）。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reset_digest_rss_items()
    notifier = NotifyHub(settings)
    sources = build_sources()
    if not sources:
        logger.warning("未設定任何新聞來源，請檢查 .env（CNYES_ENABLED / RSS）")
        return

    logger.info("開始擷取新聞 digest，來源數：%d", len(sources))
    blocks: List[str] = []
    for src in sources:
        try:
            items = src.fetch_top(settings.top_n)
        except Exception as e:
            logger.exception("來源 %s 擷取失敗: %s", src.name, e)
            notifier.push_text_chunks(
                f"❌ 財經新聞 digest 錯誤\n\n時間：{ts}\n來源：{src.name}\n錯誤：{e}"
            )
            continue

        # macro 用：保留全部 RSS items（國際總體摘要仍需多區域素材）
        if isinstance(src, RssFeedSource):
            set_digest_rss_items(items)

        # 精簡為 High + 台媒，避免月配額過早耗盡
        line_items = [it for it in items if _is_line_eligible(it)]
        if not line_items:
            logger.info("來源 %s 精簡後無可推送項目", src.name)
            continue
        blocks.append(_format_block(src, line_items, ts))

    if blocks:
        # 所有來源合併成一則訊息，push_text_chunks 在超長時自動分批
        combined = f"📰 財經新聞摘要\n{'=' * 24}\n\n" + "\n\n".join(blocks)
        if not notifier.push_text_chunks(combined):
            logger.error("digest 合併訊息 LINE 發送失敗")

    logger.info("digest 完成")


def run_morning_sequence(*, force_premarket: bool = False) -> None:
    """08:00：先 digest，再盤前台股簡報（digest 內已更新 RSS 供 macro 使用）。"""
    run_digest()
    if settings.tw_briefing_enabled:
        try:
            run_premarket(settings, force=force_premarket)
        except Exception as e:
            logger.exception("盤前簡報失敗: %s", e)
            NotifyHub(settings).push_text_chunks(f"❌ 台股盤前簡報執行錯誤\n\n{e}")


def run_postmarket_briefing(*, force: bool = False) -> None:
    """盤後台股總結（時間以 settings.tw_postmarket_hour/minute 為準）。"""
    if not settings.tw_briefing_enabled:
        logger.info("TW_BRIEFING_ENABLED=false，略過盤後排程")
        return
    try:
        run_postmarket(settings, force=force)
    except Exception as e:
        logger.exception("盤後總結失敗: %s", e)
        NotifyHub(settings).push_text_chunks(f"❌ 台股盤後總結執行錯誤\n\n{e}")


class NewsScheduler:
    """每日 08:00 晨間序列、20:00 digest、盤後台股總結（依 .env 設定，Asia/Taipei）。"""

    def __init__(self) -> None:
        self._sched = BlockingScheduler()

    def start(self) -> None:
        self._sched.add_job(
            run_morning_sequence,
            CronTrigger(hour=8, minute=0, timezone="Asia/Taipei"),
            id="financial_news_morning",
            name="晨間 digest + 台股盤前",
            replace_existing=True,
        )
        self._sched.add_job(
            run_digest,
            CronTrigger(hour=20, minute=0, timezone="Asia/Taipei"),
            id="financial_news_digest_evening",
            name="財經新聞 digest（晚間）",
            replace_existing=True,
        )
        post_hour = settings.tw_postmarket_hour
        post_minute = settings.tw_postmarket_minute
        self._sched.add_job(
            run_postmarket_briefing,
            CronTrigger(hour=post_hour, minute=post_minute, timezone="Asia/Taipei"),
            id="tw_postmarket_briefing",
            name="台股盤後總結",
            replace_existing=True,
        )
        logger.info(
            "排程已註冊：08:00 晨間 digest+盤前、20:00 digest、%02d:%02d 盤後（Asia/Taipei；"
            "FinMind 官方 17:30 公布當日 K 線，實測約 14:30 前後通常可用，搭配重試機制）",
            post_hour,
            post_minute,
        )
        if settings.run_on_start:
            logger.info("RUN_ON_START=true，立即執行一次晨間序列")
            run_morning_sequence()
        logger.info("按 Ctrl+C 停止")
        try:
            self._sched.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("正在關閉排程…")
            self._sched.shutdown()
            logger.info("排程已關閉")
