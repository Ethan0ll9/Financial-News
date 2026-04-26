"""盤前台股簡報：組資料 → PNG / HTML / Flex，推播 LINE 並寫入 briefing state。"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from config.settings import Settings
from financial_news.image_uploader import ImageUploader
from financial_news.line_notifier import LineNotifier
from financial_news.tw_briefing.briefing_state import (
    BriefingEventRecord,
    BriefingState,
    save_state,
    utc_now_iso,
)
from financial_news.tw_briefing.chart_builder import DashboardInput, render_dashboard_png
from financial_news.tw_briefing.digest_context import get_digest_rss_items
from financial_news.tw_briefing.exdividend import ExDividendEvent, events_in_date_range, events_on_date, fetch_twt48u_all
from financial_news.tw_briefing.finmind_client import FinMindClient
from financial_news.tw_briefing.flex_builder import build_briefing_bubble
from financial_news.tw_briefing.html_report import HtmlReportData, write_html_report
from financial_news.tw_briefing.macro_from_rss import format_macro_from_rss, format_tw_event_hints_from_rss
from financial_news.tw_briefing.market_queries import (
    format_sector_strength,
    format_turnover_detail,
    gather_proxy_stats,
    stock_bars,
    stock_meta_map,
    weighted_index_bars,
)
from financial_news.tw_briefing.market_text import describe_index_session
from financial_news.tw_briefing.official_events import parse_suspended_row
from financial_news.tw_briefing.theme_detect import build_market_digest
from financial_news.tw_briefing.tw_calendar import TwCalendar
from financial_news.utils import setup_logger

logger = setup_logger(__name__)
_TZ_TW = ZoneInfo("Asia/Taipei")


def _format_weekly_table(
    cal: TwCalendar,
    week_days: List[date],
    tw48_rows: list,
    suspended_rows: list,
    max_per_day: int,
) -> str:
    lines: List[str] = ["【本週事件總覽（除權息／停復牌）】", ""]
    if not week_days:
        lines.append("（本週無交易日）")
        return "\n".join(lines)

    monday = week_days[0] - timedelta(days=week_days[0].weekday()) if week_days else None
    end_w = monday + timedelta(days=4) if monday else week_days[-1]
    assert monday is not None
    ex_week = events_in_date_range(tw48_rows, monday, end_w)
    ex_by_day: Dict[date, List[ExDividendEvent]] = defaultdict(list)
    for e in ex_week:
        if monday <= e.ex_date <= end_w:
            ex_by_day[e.ex_date].append(e)

    sus: List = []
    for row in suspended_rows:
        ev = parse_suspended_row(row)
        if ev:
            sus.append(ev)

    for d in week_days:
        if not cal.is_trading_day(d):
            continue
        lines.append(f"— {d.isoformat()}（{['週一','週二','週三','週四','週五'][d.weekday()]}）")
        chunk: List[str] = []
        for e in ex_by_day.get(d, [])[:max_per_day]:
            chunk.append(f"・除權息｜{e.stock_id} {e.stock_name}｜{e.note}")
        for ev in sus:
            try:
                ad = datetime.strptime(ev.announce_date[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if ad == d:
                chunk.append(
                    f"・停復牌｜{ev.stock_id} 公告日 {ev.announce_date} → 復牌日 {ev.resumption_date or '—'}"
                )
        if not chunk:
            chunk.append("・（無 TWSE 除權息預告／暫無停復牌列管）")
        lines.extend(chunk[:max_per_day])
        if len(chunk) > max_per_day:
            lines.append(f"  …等共 {len(chunk)} 筆")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_today_events(
    today: date,
    tw48_rows: list,
    suspended_parsed: List,
) -> str:
    lines = ["【今日重點（除權息／停復牌）】", ""]
    for e in events_on_date(tw48_rows, today):
        lines.append(f"・{e.stock_id} {e.stock_name}｜{e.note}（除息日 {e.ex_date}）")
    for ev in suspended_parsed:
        try:
            ad = datetime.strptime(ev.announce_date[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if ad == today:
            lines.append(
                f"・停復牌｜{ev.stock_id} 公告 {ev.announce_date}，復牌 {ev.resumption_date or '—'}"
            )
    if len(lines) == 2:
        lines.append("（今日 TWSE 預告表無列管／或暫無資料）")
    return "\n".join(lines)


def _text_block_to_html(text: str) -> str:
    """把既有的純文字段落轉成可在 HTML 顯示的預格式化區塊。"""
    import html as _html
    return f'<pre style="font-family:inherit;font-size:12px;color:#374151;white-space:pre-wrap;margin:0">{_html.escape(text)}</pre>'


def run_premarket(settings: Settings, *, force: bool = False) -> None:
    if not settings.tw_briefing_enabled:
        logger.info("TW_BRIEFING_ENABLED=false，略過盤前簡報")
        return
    if not settings.finmind_token:
        logger.warning("FINMIND_TOKEN 未設定，略過盤前簡報")
        return

    now = datetime.now(_TZ_TW)
    today = now.date()

    notifier = LineNotifier(settings.line_channel_access_token, settings.line_user_id)
    client = FinMindClient(settings.finmind_token, cache_dir=settings.tw_state_dir)

    try:
        cal = TwCalendar.from_finmind(client)
    except Exception as e:
        logger.exception("載入交易日曆失敗: %s", e)
        notifier.push_text_chunks(f"❌ 台股盤前簡報\n\n無法載入 FinMind 交易日曆：{e}")
        return

    is_non_trading = not cal.is_trading_day(today)
    if is_non_trading:
        if not force:
            if settings.tw_non_trading_notify:
                notifier.push_text_chunks(
                    f"📅 台股盤前\n\n{today.isoformat()} 非交易日（FinMind 交易日曆），略過行情區塊。"
                )
            logger.info("%s 非交易日，略過盤前簡報", today)
            return
        logger.warning("%s 非交易日，force=True 仍產製盤前簡報（測試）", today)

    prev = cal.previous_trading_day(today)
    if prev is None:
        notifier.push_text_chunks("❌ 台股盤前簡報\n\n找不到上一交易日。")
        return

    tw48_rows = fetch_twt48u_all()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    try:
        sus_raw = client.fetch_suspended(monday.isoformat(), friday.isoformat())
    except Exception as e:
        logger.warning("TaiwanStockSuspended 略過: %s", e)
        sus_raw = []

    rss_items = get_digest_rss_items()
    macro_txt = format_macro_from_rss(rss_items)
    tw_hint = format_tw_event_hints_from_rss(rss_items)

    idx_bars, idx_id = weighted_index_bars(client, settings.tw_index_stock_id, prev, n=2)
    prev_prev = cal.previous_trading_day(prev)
    prev_close_for_idx: Optional[float] = None
    if len(idx_bars) >= 2:
        prev_close_for_idx = idx_bars[-2].close
    elif prev_prev:
        older = stock_bars(client, idx_id, prev_prev, n=1)
        if older:
            prev_close_for_idx = older[-1].close

    idx_line_text = (
        describe_index_session(idx_bars[-1], prev_close_for_idx)
        if idx_bars
        else "（無法取得加權指數 K 線；請確認 FINMIND_TOKEN，建議 TW_INDEX_STOCK_ID=TAIEX）"
    )

    meta_map = stock_meta_map(client)
    proxy_stats = gather_proxy_stats(client, prev, settings.tw_market_proxy_stocks, meta_map)
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
        ref=prev,
        k=settings.tw_top_sector_k,
        leaders_per_industry=settings.tw_sector_leaders_per_industry,
    )
    turnover_txt = format_turnover_detail(
        proxy_stats,
        ref=prev,
        top_n=settings.tw_top_turnover_n,
    )

    index_bar = idx_bars[-1] if idx_bars else None

    # PNG dashboard
    title = f"🌅 台股盤前重點（{today.isoformat()}）"
    subtitle = "盤前｜大盤、熱門族群、熱門個股"
    generated_at = utc_now_iso()
    png_path = settings.tw_report_dir / f"morning-{today.isoformat()}.png"
    html_path = settings.tw_report_dir / f"morning-{today.isoformat()}.html"

    try:
        render_dashboard_png(
            DashboardInput(
                title=title,
                subtitle=subtitle,
                session_label=str(prev),
                index_id=idx_id,
                index_bar=index_bar,
                index_prev_close=prev_close_for_idx,
                digest=digest,
                footer=f"Generated at {generated_at}",
            ),
            png_path,
        )
    except Exception as e:
        logger.exception("產生 PNG 儀表板失敗：%s", e)
        png_path = None

    # 今日／本週／國際 RSS 摘要放 extra_sections（HTML 版可見）
    sus_parsed = [x for x in (parse_suspended_row(r) for r in sus_raw) if x]
    today_events_txt = _format_today_events(today, tw48_rows, sus_parsed)
    show_weekly = (today.weekday() == 0) or (not settings.tw_weekly_briefing_only_monday)
    weekly_txt = ""
    if show_weekly:
        week_days = cal.week_trading_days_full_week(today)
        weekly_txt = _format_weekly_table(
            cal, week_days, tw48_rows, sus_raw, settings.tw_weekly_events_max_per_day
        )

    extra_sections = [
        {"title": "今日重點（除權息／停復牌）", "body_html": _text_block_to_html(today_events_txt)},
    ]
    if weekly_txt:
        extra_sections.insert(
            0, {"title": "本週事件總覽", "body_html": _text_block_to_html(weekly_txt)}
        )
    extra_sections.extend(
        [
            {"title": "族群強弱（詳）", "body_html": _text_block_to_html(sector_txt)},
            {"title": "成交活躍（詳）", "body_html": _text_block_to_html(turnover_txt)},
            {"title": "上一交易日大盤敘述", "body_html": _text_block_to_html(f"加權（{idx_id}）：{idx_line_text}")},
            {"title": "隔夜海外（RSS）", "body_html": _text_block_to_html(macro_txt)},
            {"title": "RSS 台股關鍵字提示", "body_html": _text_block_to_html(tw_hint)},
        ]
    )

    # 上傳 PNG、取得公開 URL
    image_url: Optional[str] = None
    if png_path:
        uploader = ImageUploader(settings.imgbb_api_key)
        image_url = uploader.upload(png_path)

    # HTML dashboard
    try:
        write_html_report(
            HtmlReportData(
                title=title,
                subtitle=subtitle,
                session_label=str(prev),
                index_id=idx_id,
                index_bar=index_bar,
                index_prev_close=prev_close_for_idx,
                digest=digest,
                generated_at=generated_at,
                extra_sections=extra_sections,
                image_url=image_url,
                footer=f"Generated at {generated_at}｜來源 FinMind + TWSE OpenAPI｜proxy {digest.total_members} 檔",
            ),
            html_path,
        )
    except Exception as e:
        logger.exception("產生 HTML 失敗：%s", e)

    # 推送
    pushed = False
    if settings.tw_push_mode == "visual":
        pushed = _push_visual(
            notifier=notifier,
            title=title,
            subtitle=subtitle,
            idx_id=idx_id,
            index_bar=index_bar,
            prev_close_for_idx=prev_close_for_idx,
            digest=digest,
            image_url=image_url,
            today_events_txt=today_events_txt,
            weekly_txt=weekly_txt,
            macro_txt=macro_txt,
            html_path=html_path,
            force_banner=(force and is_non_trading),
        )
    if not pushed:
        body = _build_text_fallback(
            force=force,
            is_non_trading=is_non_trading,
            today=today,
            title=title,
            generated_at=generated_at,
            macro_txt=macro_txt,
            tw_hint=tw_hint,
            weekly_txt=weekly_txt,
            today_events_txt=today_events_txt,
            prev=prev,
            idx_id=idx_id,
            idx_line_text=idx_line_text,
            sector_txt=sector_txt,
            turnover_txt=turnover_txt,
        )
        if not notifier.push_text_chunks(body):
            logger.error("盤前簡報 LINE 發送失敗（文字 fallback）")

    # state for 盤後事件驗證
    watch: List[str] = list(dict.fromkeys(settings.tw_market_proxy_stocks[:8]))
    ev_records: List[BriefingEventRecord] = []
    for e in events_on_date(tw48_rows, today):
        watch.append(e.stock_id)
        ev_records.append(
            BriefingEventRecord(
                kind="exdiv",
                stock_id=e.stock_id,
                label=e.note,
                ref_date=e.ex_date.isoformat(),
                detail=e.stock_name,
            )
        )
    for ev in sus_parsed:
        try:
            ad = datetime.strptime(ev.announce_date[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if ad == today:
            watch.append(ev.stock_id)
            ev_records.append(
                BriefingEventRecord(
                    kind="suspended",
                    stock_id=ev.stock_id,
                    label="停復牌",
                    ref_date=ad.isoformat(),
                    detail=f"復牌日 {ev.resumption_date}",
                )
            )
    watch = list(dict.fromkeys([w for w in watch if w]))

    state = BriefingState(
        session_date=today.isoformat(),
        generated_at=generated_at,
        index_stock_id=idx_id,
        watch_tickers=watch,
        events=ev_records,
    )
    try:
        save_state(settings.tw_state_dir, state)
    except OSError as e:
        logger.error("寫入 briefing state 失敗: %s", e)


def _push_visual(
    *,
    notifier: LineNotifier,
    title: str,
    subtitle: str,
    idx_id: str,
    index_bar,
    prev_close_for_idx,
    digest,
    image_url: Optional[str],
    today_events_txt: str,
    weekly_txt: str,
    macro_txt: str,
    html_path,
    force_banner: bool,
) -> bool:
    messages: List[dict] = []

    bubble = build_briefing_bubble(
        title=("【測試】" + title) if force_banner else title,
        subtitle=subtitle,
        index_id=idx_id,
        index_bar=index_bar,
        index_prev_close=prev_close_for_idx,
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
        tail_lines.append("【測試】本日非 FinMind 交易日；大盤／族群以上一交易日收盤為準。")
    if weekly_txt:
        tail_lines.append(weekly_txt)
    tail_lines.append(today_events_txt)
    tail_lines.append(macro_txt)
    tail_lines.append(f"📎 本機儀表板：{html_path}")
    tail_text = "\n\n".join([t for t in tail_lines if t])
    if tail_text:
        messages.append({"type": "text", "text": tail_text[:4500]})

    return notifier.push_messages(messages)


def _build_text_fallback(
    *,
    force: bool,
    is_non_trading: bool,
    today: date,
    title: str,
    generated_at: str,
    macro_txt: str,
    tw_hint: str,
    weekly_txt: str,
    today_events_txt: str,
    prev: date,
    idx_id: str,
    idx_line_text: str,
    sector_txt: str,
    turnover_txt: str,
) -> str:
    parts: List[str] = []
    if force and is_non_trading:
        parts.extend(
            [
                "【測試】本日非 FinMind 交易日曆之交易日，仍產製簡報。",
                "大盤／族群／成交以上一交易日收盤為準；「今日重點」仍以曆法今日對照 TWSE 預告。",
                "",
            ]
        )
    parts.extend(
        [
            title,
            f"資料時間：{generated_at}",
            "",
            macro_txt,
            "",
            tw_hint,
            "",
        ]
    )
    if weekly_txt:
        parts.append(weekly_txt)
        parts.append("")
    parts.append(today_events_txt)
    parts.append("")
    parts.append(f"【上個交易日（{prev}）大盤摘要】")
    parts.append(f"加權（{idx_id}）：{idx_line_text}")
    parts.append("")
    parts.append(sector_txt)
    parts.append("")
    parts.append(turnover_txt)
    return "\n".join(parts)
