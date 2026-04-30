"""財經新聞 digest：長駐排程或單次執行。"""
from __future__ import annotations

import argparse

from financial_news.scheduler import NewsScheduler, run_digest, run_morning_sequence, run_postmarket_briefing
from financial_news.utils import setup_logger

logger = setup_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="財經新聞熱門／RSS digest + LINE push")
    parser.add_argument(
        "--digest",
        action="store_true",
        help="單次執行 run_digest() 後結束（僅新聞；與排程 20:00 相同）",
    )
    parser.add_argument(
        "--morning",
        action="store_true",
        help="單次執行 run_morning_sequence() 後結束（digest → 盤前；與排程 08:00 相同）",
    )
    parser.add_argument(
        "--postmarket-only",
        action="store_true",
        help="單次僅執行台股盤後總結（邏輯與排程相同，時間依 TW_POSTMARKET_HOUR/MINUTE；仍受 TW_BRIEFING_ENABLED 控制）",
    )
    parser.add_argument(
        "--day-once",
        action="store_true",
        help="單次晨間流程後再接盤後（測試整日管線）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="搭配 --morning／--day-once／--postmarket-only：非交易日仍跑台股簡報（僅本機／測試）",
    )
    args = parser.parse_args()

    if args.force and not (args.morning or args.day_once or args.postmarket_only):
        parser.error("--force 須與 --morning、--day-once 或 --postmarket-only 併用")

    single_flags = (args.digest, args.morning, args.postmarket_only, args.day_once)
    if sum(1 for f in single_flags if f) > 1:
        parser.error("請只擇一：--digest / --morning / --postmarket-only / --day-once")

    if args.digest:
        if args.force:
            parser.error("--digest 不支援 --force")
        logger.info("模式：單次 run_digest（--digest）")
        run_digest()
        return
    if args.morning:
        logger.info("模式：單次 run_morning_sequence（--morning）force=%s", args.force)
        run_morning_sequence(force_premarket=args.force)
        return
    if args.postmarket_only:
        logger.info("模式：單次盤後（--postmarket-only）force=%s", args.force)
        run_postmarket_briefing(force=args.force)
        return
    if args.day_once:
        logger.info("模式：單次整日管線（--day-once）force=%s", args.force)
        run_morning_sequence(force_premarket=args.force)
        run_postmarket_briefing(force=args.force)
        return

    logger.info("模式：長駐排程")
    NewsScheduler().start()


if __name__ == "__main__":
    main()
