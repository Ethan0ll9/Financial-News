"""財經新聞 digest：長駐排程或單次執行。"""
from __future__ import annotations

import argparse

from financial_news.scheduler import NewsScheduler, run_digest
from financial_news.utils import setup_logger

logger = setup_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="財經新聞熱門／RSS digest + LINE push")
    parser.add_argument(
        "--once",
        action="store_true",
        help="只執行一次 digest 後結束（適合工作排程器）",
    )
    args = parser.parse_args()

    if args.once:
        logger.info("模式：單次執行（--once）")
        run_digest()
        return

    logger.info("模式：長駐排程")
    NewsScheduler().start()


if __name__ == "__main__":
    main()
