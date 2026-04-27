"""定時擷取新聞並推播 LINE。"""
from __future__ import annotations

from datetime import datetime
from typing import List

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import settings
from financial_news.line_notifier import LineNotifier
from financial_news.models import NewsItem
from financial_news.sources.base import NewsSource
from financial_news.sources.cnyes import CnyesPopularSource
from financial_news.sources.rss_feed import RssFeedSource
from financial_news.tw_briefing.digest_context import reset_digest_rss_items, set_digest_rss_items
from financial_news.tw_briefing.premarket_report import run_premarket
from financial_news.tw_briefing.postmarket_report import run_postmarket
from financial_news.utils import setup_logger, strip_html

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
    """擷取所有啟用來源並推播（單次）。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reset_digest_rss_items()
    notifier = LineNotifier(
        settings.line_channel_access_token,
        settings.line_user_id,
    )
    sources = build_sources()
    if not sources:
        logger.warning("未設定任何新聞來源，請檢查 .env（CNYES_ENABLED / RSS）")
        return

    logger.info("開始擷取新聞 digest，來源數：%d", len(sources))
    for src in sources:
        try:
            items = src.fetch_top(settings.top_n)
        except Exception as e:
            logger.exception("來源 %s 擷取失敗: %s", src.name, e)
            err_msg = f"❌ 財經新聞 digest 錯誤\n\n時間：{ts}\n來源：{src.name}\n錯誤：{e}"
            notifier.push_text_chunks(err_msg)
            continue

        body = _format_block(src, items, ts)
        header = f"📰 財經新聞摘要\n{'=' * 24}\n\n"
        if not notifier.push_text_chunks(header + body):
            logger.error("來源 %s LINE 發送失敗", src.name)
        if isinstance(src, RssFeedSource):
            set_digest_rss_items(items)

    logger.info("digest 完成")


def run_morning_sequence(*, force_premarket: bool = False) -> None:
    """08:00：先 digest，再盤前台股簡報（digest 內已更新 RSS 供 macro 使用）。"""
    run_digest()
    if settings.tw_briefing_enabled:
        try:
            run_premarket(settings, force=force_premarket)
        except Exception as e:
            logger.exception("盤前簡報失敗: %s", e)
            LineNotifier(
                settings.line_channel_access_token,
                settings.line_user_id,
            ).push_text_chunks(f"❌ 台股盤前簡報執行錯誤\n\n{e}")


def run_postmarket_briefing(*, force: bool = False) -> None:
    """盤後台股總結（時間以 settings.tw_postmarket_hour/minute 為準）。"""
    if not settings.tw_briefing_enabled:
        logger.info("TW_BRIEFING_ENABLED=false，略過盤後排程")
        return
    try:
        run_postmarket(settings, force=force)
    except Exception as e:
        logger.exception("盤後總結失敗: %s", e)
        LineNotifier(
            settings.line_channel_access_token,
            settings.line_user_id,
        ).push_text_chunks(f"❌ 台股盤後總結執行錯誤\n\n{e}")


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
            "FinMind 官方 17:30 公布當日 K 線，實測 14:00 後通常可用，搭配重試機制）",
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
