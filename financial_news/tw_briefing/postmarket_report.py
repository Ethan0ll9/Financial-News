"""盤後走勢總結：組資料 → PNG / HTML / Flex；從盤前 state 做事件驗證。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from config.settings import Settings
from financial_news.image_uploader import ImageUploader
from financial_news.line_notifier import LineNotifier
from financial_news.tw_briefing.briefing_state import BriefingState, load_state, utc_now_iso
from financial_news.tw_briefing.chart_builder import DashboardInput, render_dashboard_png
from financial_news.tw_briefing.finmind_client import FinMindClient
from financial_news.tw_briefing.flex_builder import build_briefing_bubble
from financial_news.tw_briefing.html_report import HtmlReportData, write_html_report
from financial_news.tw_briefing.market_queries import (
    StockMeta,
    format_sector_strength,
    format_turnover_detail,
    gather_proxy_stats,
    stock_bars,
    stock_meta_map,
    weighted_index_bars,
)
from financial_news.tw_briefing.market_text import describe_index_session, pct_change
from financial_news.tw_briefing.theme_detect import build_market_digest
from financial_news.tw_briefing.tw_calendar import TwCalendar
from financial_news.utils import setup_logger

logger = setup_logger(__name__)
_TZ_TW = ZoneInfo("Asia/Taipei")


def _event_verify_text(
    client: FinMindClient,
    session: date,
    state: Optional[BriefingState],
    meta_map: Optional[Dict[str, StockMeta]] = None,
) -> str:
    lines = ["【事件驗證（盤前關注 vs 當日收盤）】", ""]
    if not state or not state.events:
        lines.append("（無盤前 state 或當日無列管事件；若剛啟用模組屬正常）")
        return "\n".join(lines)
    tickers = list(dict.fromkeys([e.stock_id for e in state.events if e.stock_id]))
    for tid in tickers:
        bars = stock_bars(client, tid, session, n=2)
        name = ""
        if meta_map and tid in meta_map:
            name = meta_map[tid].name
        label = f"{tid} {name}" if name else tid
        if len(bars) < 2:
            lines.append(f"・{label}：資料不足")
            continue
        a, b = bars[-2], bars[-1]
        r = pct_change(a.close, b.close)
        sign = "+" if r >= 0 else ""
        lines.append(f"・{label} 收 {b.close:,.2f}（{sign}{r:.2f}%）")
    return "\n".join(lines)


def _text_block_to_html(text: str) -> str:
    import html as _html
    return f'<pre style="font-family:inherit;font-size:12px;color:#374151;white-space:pre-wrap;margin:0">{_html.escape(text)}</pre>'


def run_postmarket(settings: Settings, *, force: bool = False) -> None:
    if not settings.tw_briefing_enabled:
        logger.info("TW_BRIEFING_ENABLED=false，略過盤後總結")
        return
    if not settings.finmind_token:
        logger.warning("FINMIND_TOKEN 未設定，略過盤後總結")
        return

    now = datetime.now(_TZ_TW)
    today = now.date()
    notifier = LineNotifier(settings.line_channel_access_token, settings.line_user_id)
    client = FinMindClient(settings.finmind_token, cache_dir=settings.tw_state_dir)

    try:
        cal = TwCalendar.from_finmind(client)
    except Exception as e:
        logger.exception("載入交易日曆失敗: %s", e)
        notifier.push_text_chunks(f"❌ 台股盤後總結\n\n無法載入 FinMind 交易日曆：{e}")
        return

    is_non_trading = not cal.is_trading_day(today)
    if is_non_trading:
        if not force:
            if settings.tw_non_trading_notify:
                notifier.push_text_chunks(f"📅 台股盤後\n\n{today.isoformat()} 非交易日，略過。")
            logger.info("%s 非交易日，略過盤後", today)
            return
        logger.warning("%s 非交易日，force=True 仍產製盤後（測試）", today)

    prev = cal.previous_trading_day(today)
    idx_bars, idx_id = weighted_index_bars(client, settings.tw_index_stock_id, today, n=2)
    prev_close: Optional[float] = None
    if len(idx_bars) >= 2:
        prev_close = idx_bars[-2].close
    elif prev:
        pb = stock_bars(client, idx_id, prev, n=1)
        if pb:
            prev_close = pb[-1].close

    idx_txt = (
        describe_index_session(idx_bars[-1], prev_close)
        if idx_bars
        else "（無法取得加權指數當日 K 線；建議 TW_INDEX_STOCK_ID=TAIEX）"
    )

    meta_map = stock_meta_map(client)
    proxy_stats = gather_proxy_stats(client, today, settings.tw_market_proxy_stocks, meta_map)
    digest = build_market_digest(
        proxy_stats,
        hot_themes_k=settings.tw_hot_themes_k,
        cold_themes_k=settings.tw_top_sector_k,
        top_gainers_n=settings.tw_hot_gainers_n,
        top_losers_n=settings.tw_hot_losers_n,
        top_turnover_n=settings.tw_top_turnover_n,
    )

    sector_txt = format_sector_strength(
        proxy_stats,
        ref=today,
        k=settings.tw_top_sector_k,
        leaders_per_industry=settings.tw_sector_leaders_per_industry,
    )
    turnover_txt = format_turnover_detail(
        proxy_stats,
        ref=today,
        top_n=settings.tw_top_turnover_n,
    )

    state = load_state(settings.tw_state_dir, today.isoformat())
    verify_txt = _event_verify_text(client, today, state, meta_map=meta_map)

    title = f"🌇 台股盤中走勢總結（{today.isoformat()}）"
    subtitle = "盤後｜大盤、熱門族群、熱門個股、事件驗證"
    generated_at = utc_now_iso()
    png_path = settings.tw_report_dir / f"postmarket-{today.isoformat()}.png"
    html_path = settings.tw_report_dir / f"postmarket-{today.isoformat()}.html"

    index_bar = idx_bars[-1] if idx_bars else None

    try:
        render_dashboard_png(
            DashboardInput(
                title=title,
                subtitle=subtitle,
                session_label=today.isoformat(),
                index_id=idx_id,
                index_bar=index_bar,
                index_prev_close=prev_close,
                digest=digest,
                footer=f"Generated at {generated_at}",
            ),
            png_path,
        )
    except Exception as e:
        logger.exception("產生 PNG 儀表板失敗：%s", e)
        png_path = None

    extra_sections = [
        {"title": "事件驗證", "body_html": _text_block_to_html(verify_txt)},
        {"title": "族群強弱（詳）", "body_html": _text_block_to_html(sector_txt)},
        {"title": "成交活躍（詳）", "body_html": _text_block_to_html(turnover_txt)},
        {"title": "指數敘述", "body_html": _text_block_to_html(f"加權（{idx_id}）：{idx_txt}")},
    ]

    image_url: Optional[str] = None
    if png_path:
        uploader = ImageUploader(settings.imgbb_api_key)
        image_url = uploader.upload(png_path)

    try:
        write_html_report(
            HtmlReportData(
                title=title,
                subtitle=subtitle,
                session_label=today.isoformat(),
                index_id=idx_id,
                index_bar=index_bar,
                index_prev_close=prev_close,
                digest=digest,
                generated_at=generated_at,
                extra_sections=extra_sections,
                image_url=image_url,
                footer=f"Generated at {generated_at}｜來源 FinMind｜proxy {digest.total_members} 檔",
            ),
            html_path,
        )
    except Exception as e:
        logger.exception("產生 HTML 失敗：%s", e)

    pushed = False
    if settings.tw_push_mode == "visual":
        pushed = _push_visual(
            notifier=notifier,
            title=title,
            subtitle=subtitle,
            idx_id=idx_id,
            index_bar=index_bar,
            prev_close=prev_close,
            digest=digest,
            image_url=image_url,
            verify_txt=verify_txt,
            html_path=html_path,
            force_banner=(force and is_non_trading),
        )
    if not pushed:
        body = _build_text_fallback(
            force=force,
            is_non_trading=is_non_trading,
            title=title,
            generated_at=generated_at,
            idx_id=idx_id,
            idx_txt=idx_txt,
            sector_txt=sector_txt,
            turnover_txt=turnover_txt,
            verify_txt=verify_txt,
        )
        if not notifier.push_text_chunks(body):
            logger.error("盤後總結 LINE 發送失敗（文字 fallback）")


def _push_visual(
    *,
    notifier: LineNotifier,
    title: str,
    subtitle: str,
    idx_id: str,
    index_bar,
    prev_close,
    digest,
    image_url: Optional[str],
    verify_txt: str,
    html_path,
    force_banner: bool,
) -> bool:
    messages: List[dict] = []

    bubble = build_briefing_bubble(
        title=("【測試】" + title) if force_banner else title,
        subtitle=subtitle,
        index_id=idx_id,
        index_bar=index_bar,
        index_prev_close=prev_close,
        digest=digest,
        image_url=image_url,
    )
    messages.append({"type": "flex", "altText": title[:400], "contents": bubble})

    if image_url:
        messages.append(
            {
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            }
        )

    tail_lines: List[str] = []
    if force_banner:
        tail_lines.append("【測試】本日非交易日；若無當日 K 線為正常。")
    tail_lines.append(verify_txt)
    tail_lines.append(f"📎 本機儀表板：{html_path}")
    tail_text = "\n\n".join([t for t in tail_lines if t])
    if tail_text:
        messages.append({"type": "text", "text": tail_text[:4500]})

    return notifier.push_messages(messages)


def _build_text_fallback(
    *,
    force: bool,
    is_non_trading: bool,
    title: str,
    generated_at: str,
    idx_id: str,
    idx_txt: str,
    sector_txt: str,
    turnover_txt: str,
    verify_txt: str,
) -> str:
    parts: List[str] = []
    if force and is_non_trading:
        parts.append("【測試】本日非交易日；以下若無當日 K 線為正常。")
        parts.append("")
    parts.extend(
        [
            title,
            f"資料時間：{generated_at}",
            "",
            f"【大盤】加權（{idx_id}）",
            idx_txt,
            "",
            sector_txt,
            "",
            turnover_txt,
            "",
            verify_txt,
        ]
    )
    return "\n".join(parts)
