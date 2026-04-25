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

    logger.info("digest 完成")


class NewsScheduler:
    """每日 08:00、20:00（Asia/Taipei）執行。"""

    def __init__(self) -> None:
        self._sched = BlockingScheduler()

    def start(self) -> None:
        self._sched.add_job(
            run_digest,
            CronTrigger(hour="8,20", minute="0", timezone="Asia/Taipei"),
            id="financial_news_digest",
            name="財經新聞 digest",
            replace_existing=True,
        )
        logger.info("排程已註冊：每日 08:00、20:00（Asia/Taipei）")
        if settings.run_on_start:
            logger.info("RUN_ON_START=true，立即執行一次 digest")
            run_digest()
        logger.info("按 Ctrl+C 停止")
        try:
            self._sched.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("正在關閉排程…")
            self._sched.shutdown()
            logger.info("排程已關閉")
