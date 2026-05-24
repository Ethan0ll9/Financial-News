"""盤後走勢總結：組資料 → PNG / HTML / Flex；從盤前 state 做事件驗證。"""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from config.settings import Settings
from financial_news.image_uploader import ImageUploader
from financial_news.line_notifier import LineNotifier
from financial_news.tw_briefing.briefing_state import (
    BriefingEventRecord,
    BriefingState,
    load_state,
    utc_now_iso,
)
from financial_news.tw_briefing.chart_builder import DashboardInput, render_dashboard_png
from financial_news.tw_briefing.event_window import (
    collect_lookahead,
    format_next_day_brief,
)
from financial_news.tw_briefing.exdividend import fetch_twt48u_all
from financial_news.tw_briefing.finmind_client import FinMindClient, IndexBar
from financial_news.tw_briefing.flex_builder import build_briefing_bubble
from financial_news.tw_briefing.html_report import (
    HtmlReportData,
    text_block_to_html as _text_block_to_html,
    write_html_report,
)
from financial_news.tw_briefing.official_events import parse_suspended_row
from financial_news.tw_briefing.twse_announcements import fetch_shareholder_meetings
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
from financial_news.tw_briefing.twse_market import market_totals_on
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)
_TZ_TW = ZoneInfo("Asia/Taipei")


_KIND_LABEL = {
    "exdiv": "🟢 除權息",
    "suspended": "⛔ 暫停交易",
    "attention": "⚠️ 注意股",
    "disposal": "🚫 處置股",
    "shareholder_meeting": "📋 股東會",
    "short_cover": "🔁 融券強制回補",
    "earnings": "💰 財報公告",
    "conference": "🎤 法說會",
    "material_news": "📣 重大訊息",
}


def _event_verify_text(
    client: FinMindClient,
    session: date,
    state: Optional[BriefingState],
    meta_map: Optional[Dict[str, StockMeta]] = None,
) -> str:
    lines = ["【事件驗證（盤前列管 vs 當日收盤）】", ""]
    if not state or not state.events:
        lines.append("（無盤前 state 或當日無列管事件；若剛啟用模組屬正常）")
        return "\n".join(lines)

    # 依 kind 分組，保留輸入順序
    by_kind: Dict[str, List[BriefingEventRecord]] = {}
    for ev in state.events:
        if not ev.stock_id:
            continue
        by_kind.setdefault(ev.kind or "other", []).append(ev)

    # 收盤資料以代號 cache，避免同一檔多事件重抓
    pct_cache: Dict[str, Optional[tuple]] = {}

    def _fetch_pct(sid: str):
        if sid in pct_cache:
            return pct_cache[sid]
        bars = stock_bars(client, sid, session, n=2)
        if len(bars) < 2:
            pct_cache[sid] = None
            return None
        a, b = bars[-2], bars[-1]
        r = pct_change(a.close, b.close)
        pct_cache[sid] = (b.close, r)
        return pct_cache[sid]

    kind_order = [
        "earnings",
        "shareholder_meeting",
        "conference",
        "material_news",
        "short_cover",
        "exdiv",
        "suspended",
        "attention",
        "disposal",
    ]
    seen_kinds = [k for k in kind_order if k in by_kind] + [
        k for k in by_kind if k not in kind_order
    ]

    for k in seen_kinds:
        head = _KIND_LABEL.get(k, k)
        lines.append(f"— {head}（{len(by_kind[k])} 檔）")
        for ev in by_kind[k]:
            sid = ev.stock_id
            name = ""
            if meta_map and sid in meta_map:
                name = meta_map[sid].name
            label = f"{sid} {name}" if name else sid
            extra = f"｜{ev.label}" if ev.label else ""
            res = _fetch_pct(sid)
            if not res:
                lines.append(f"・{label}{extra}：資料不足")
                continue
            close, r = res
            sign = "+" if r >= 0 else ""
            lines.append(f"・{label}{extra} 收 {close:,.2f}（{sign}{r:.2f}%）")
        lines.append("")
    return "\n".join(lines).rstrip()




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

    # 嚴格驗日：在實際交易日才啟用；force 跑非交易日測試時關閉，避免怎麼試都跑不完。
    strict_today = settings.tw_postmarket_strict_today and not (force and is_non_trading)

    idx_bars, idx_id = _fetch_index_with_retry(
        client=client,
        configured_id=settings.tw_index_stock_id,
        today=today,
        strict_today=strict_today,
        max_retries=settings.tw_postmarket_max_retries,
        retry_interval_min=settings.tw_postmarket_retry_interval_min,
    )

    if strict_today and not idx_bars:
        msg = (
            f"⏳ 台股盤後總結\n\n"
            f"{today.isoformat()} 加權指數當日 K 線尚未由 FinMind 公布"
            f"（官方更新時間 17:30）。\n"
            f"已重試 {settings.tw_postmarket_max_retries} 次（每次間隔 "
            f"{settings.tw_postmarket_retry_interval_min} 分鐘）仍無資料，本次略過。\n"
            f"如需更晚的排程時間，請調整 .env：TW_POSTMARKET_HOUR / TW_POSTMARKET_MINUTE。"
        )
        notifier.push_text_chunks(msg)
        logger.warning("盤後：今日 idx_bars 重試後仍未取得，已通知並結束")
        return

    prev_close: Optional[float] = None
    if len(idx_bars) >= 2:
        prev_close = idx_bars[-2].close
    elif prev:
        pb = stock_bars(client, idx_id, prev, n=1)
        if pb:
            prev_close = pb[-1].close

    # 用 TWSE FMTQIK 補全大盤總成交與漲跌點數（FinMind TaiwanStockPrice
    # 對 TAIEX 不會回 trading_money/volume，且無精確漲跌點）
    if idx_bars:
        totals = market_totals_on(today)
        if totals is not None:
            last = idx_bars[-1]
            new_last = replace(
                last,
                trading_money=totals.trade_value_yuan or last.trading_money,
                volume=totals.trade_volume_shares or last.volume,
            )
            idx_bars = idx_bars[:-1] + [new_last]
            if (not prev_close) and totals.taiex_close and totals.change_pts is not None:
                prev_close = totals.taiex_close - totals.change_pts
        else:
            logger.warning("FMTQIK 未取得 %s 之大盤總成交，將顯示『資料缺』", today)

    idx_txt = (
        describe_index_session(idx_bars[-1], prev_close)
        if idx_bars
        else "（無法取得加權指數當日 K 線；建議 TW_INDEX_STOCK_ID=TAIEX）"
    )

    meta_map = stock_meta_map(client)
    proxy_stats = gather_proxy_stats(
        client,
        today,
        settings.tw_market_proxy_stocks,
        meta_map,
        strict_today=strict_today,
    )
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

    # 明日預告（D-1 精簡列；輕量再抓一次 TWSE 公告以拿最新狀態）
    next_day_txt = ""
    if settings.tw_postmarket_show_next_day:
        next_td = cal.next_trading_day(today)
        try:
            tw48_rows = fetch_twt48u_all()
            # TaiwanStockSuspended 需要付費方案；失敗則略過停復牌部分，不影響其餘預告
            try:
                sus_rows = client.fetch_suspended(today.isoformat(), next_td.isoformat())
                sus_parsed = [x for x in (parse_suspended_row(r) for r in sus_rows) if x]
            except Exception as _sus_err:
                logger.debug("TaiwanStockSuspended 無法取得（可能需付費方案），跳過：%s", _sus_err)
                sus_parsed = []
            sh_meetings = fetch_shareholder_meetings()
            la_items = collect_lookahead(
                base_date=today,
                cal=cal,
                n_trading_days=1,
                include_kinds=settings.tw_events_lookahead_kinds,
                tw48_rows=tw48_rows,
                sh_meetings=sh_meetings,
                suspended=sus_parsed,
            )
            next_day_txt = format_next_day_brief(la_items, next_trading_day=next_td)
        except Exception as e:  # 任何下游 API 失敗都不要影響盤後本體
            logger.warning("明日預告產製失敗，已忽略：%s", e)

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
    ]
    if next_day_txt:
        extra_sections.append(
            {"title": "明日預告", "body_html": _text_block_to_html(next_day_txt)}
        )
    extra_sections.extend(
        [
            {"title": "族群強弱（詳）", "body_html": _text_block_to_html(sector_txt)},
            {"title": "成交活躍（詳）", "body_html": _text_block_to_html(turnover_txt)},
            {"title": "指數敘述", "body_html": _text_block_to_html(f"加權（{idx_id}）：{idx_txt}")},
        ]
    )

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
            next_day_txt=next_day_txt,
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
            next_day_txt=next_day_txt,
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
    next_day_txt: str,
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

    # 移除重複的獨立 image 訊息：Flex bubble 的 hero 已含同一張儀表板圖，
    # 額外推一則 image 會佔用 LINE 月配額且畫面重複（user reported 2026-05）。

    # 送 flex（單則訊息即可）
    ok = notifier.push_messages(messages)

    # tail 文字以 push_text_chunks 分批送，避免截斷
    tail_lines: List[str] = []
    if force_banner:
        tail_lines.append("【測試】本日非交易日；若無當日 K 線為正常。")
    tail_lines.append(verify_txt)
    if next_day_txt:
        tail_lines.append(next_day_txt)
    tail_lines.append(f"📎 本機儀表板：{html_path}")
    tail_text = "\n\n".join([t for t in tail_lines if t])
    if tail_text:
        if not notifier.push_text_chunks(tail_text):
            ok = False

    return ok


def _fetch_index_with_retry(
    *,
    client: FinMindClient,
    configured_id: str,
    today: date,
    strict_today: bool,
    max_retries: int,
    retry_interval_min: int,
) -> Tuple[list, str]:
    """嘗試取當日加權指數 K 線；strict 模式下若無當日資料則重試。

    回傳 ``(bars, idx_id)``：
    - 非 strict：取最後可用的 K 線（可能是上一交易日，向下相容舊行為）。
    - strict：必拿 ``day == today`` 的 K 線；重試耗盡仍無則回傳空列。
    """
    attempts = max(1, max_retries + 1) if strict_today else 1
    bars: list = []
    idx_id = ""
    for i in range(attempts):
        bars, idx_id = weighted_index_bars(
            client, configured_id, today, n=2, strict_today=strict_today
        )
        if bars or not strict_today:
            return bars, idx_id
        if i + 1 >= attempts:
            break
        wait_sec = max(1, retry_interval_min) * 60
        logger.warning(
            "盤後：當日 K 線尚未公布，第 %d/%d 次重試將於 %d 秒後執行",
            i + 1,
            attempts - 1,
            wait_sec,
        )
        time.sleep(wait_sec)
    return bars, idx_id


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
    next_day_txt: str = "",
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
    if next_day_txt:
        parts.append("")
        parts.append(next_day_txt)
    return "\n".join(parts)
