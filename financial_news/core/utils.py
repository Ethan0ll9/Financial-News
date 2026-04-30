"""日誌工具。"""
import html
import logging
import re
import sys

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """移除常見 HTML 標籤並做實體解碼（RSS 標題用）。"""
    if not text:
        return ""
    t = _HTML_TAG_RE.sub("", str(text))
    return html.unescape(t).strip()


def setup_logger(name: str = __name__, log_level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger
