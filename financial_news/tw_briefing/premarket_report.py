"""盤前台股簡報：組資料 → PNG / HTML / Flex，推播 LINE 並寫入 briefing state。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from config.settings import Settings
from financial_news.notify_hub import NotifyHub
from financial_news.tw_briefing.briefing_state import (
    BriefingEventRecord,
    BriefingState,
    save_state,
    utc_now_iso,
)
from financial_news.image_uploader import ImageUploader
from financial_news.tw_briefing.dashboard_v2 import (
    OverviewData,
    WatchlistData,
    render_overview_png,
    render_watchlist_png,
)
from financial_news.tw_briefing.digest_context import get_digest_rss_items
from financial_news.tw_briefing.exdividend import events_on_date, fetch_twt48u_all
from financial_news.tw_briefing.finmind_client import FinMindClient
from financial_news.tw_briefing.market_breadth import (
    MarketBreadthClient,
    build_index_summary,
    build_industry_flow,
)
from financial_news.tw_briefing.html_report import (
    HtmlReportData,
    text_block_to_html as _text_block_to_html,
    write_html_report,
)
from financial_news.tw_briefing.event_window import (
    collect_in_progress,
    collect_lookahead,
    format_in_progress_block,
    format_lookahead_block,
)
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
from financial_news.tw_briefing.mops_material_news import (
    fetch_material_news,
    format_material_news_block,
)
from financial_news.tw_briefing.official_events import parse_suspended_row
from financial_news.tw_briefing.theme_detect import build_market_digest
from financial_news.tw_briefing.tw_calendar import TwCalendar
from financial_news.tw_briefing.twse_announcements import (
    AttentionStock,
    DisposalStock,
    ShareholderMeeting,
    attentions_on,
    disposals_on,
    exclude_warrants_disposal,
    fetch_attention_stocks,
    fetch_disposal_stocks,
    fetch_shareholder_meetings,
    shareholder_meetings_on,
    short_cover_on,
)
from financial_news.tw_briefing.twse_market import market_totals_on
from financial_news.core.utils import setup_logger

from dataclasses import replace

logger = setup_logger(__name__)
_TZ_TW = ZoneInfo("Asia/Taipei")


def _split_watch(items: list, watch_set: set, id_attr: str = "stock_id"):
    """把清單依 watchlist 分成 (watch_items, other_items)。"""
    w, o = [], []
    for it in items:
        (w if getattr(it, id_attr) in watch_set else o).append(it)
    return w, o


def _append_kind_block(
    lines: List[str],
    label: str,
    watch_rows: List[str],
    other_rows: List[str],
    *,
    other_max: int = 5,
) -> None:
    """把一個 kind 的 watchlist / 其他列進 lines，並加一個空行分隔。"""
    total = len(watch_rows) + len(other_rows)
    if total == 0:
        return
    lines.append(f"— {label}（{total} 筆）")
    lines.extend(watch_rows)  # watchlist 全數顯示
    shown_other = other_rows[:other_max]
    lines.extend(shown_other)
    if len(other_rows) > other_max:
        lines.append(f"  …另 {len(other_rows) - other_max} 筆（非觀察清單，略）")
    lines.append("")


def _format_today_events(
    today: date,
    tw48_rows: list,
    suspended_parsed: List,
    *,
    attention: Optional[List[AttentionStock]] = None,
    disposal: Optional[List[DisposalStock]] = None,
    sh_meetings: Optional[List[ShareholderMeeting]] = None,
    short_cover: Optional[List[ShareholderMeeting]] = None,
    watch_set: Optional[set] = None,
    other_max: int = 5,
) -> str:
    """格式化今日重點。全部 kind 均以 watchlist 優先排前面。

    ``sh_meetings`` / ``short_cover`` 應已用 watch_set 過濾過（只傳觀察清單）；
    其餘 exdiv / suspended / attention / disposal 由本函式自行分組：
    - watchlist 個股全部顯示
    - 非 watchlist 最多顯示 ``other_max`` 筆（預設 5）
    """
    ws = watch_set or set()
    lines = ["【今日重點（除權息／停復牌／注意處置／股東會／融券回補）】", ""]
    initial_len = len(lines)

    # ---- 除權息 ---------------------------------------------------------------
    all_exdiv = list(events_on_date(tw48_rows, today))
    w_exdiv = [e for e in all_exdiv if e.stock_id in ws]
    o_exdiv = [e for e in all_exdiv if e.stock_id not in ws]
    _append_kind_block(
        lines, "除權息",
        [f"・★ {e.stock_id} {e.stock_name}｜{e.note}（除息日 {e.ex_date}）" for e in w_exdiv],
        [f"・{e.stock_id} {e.stock_name}｜{e.note}（除息日 {e.ex_date}）" for e in o_exdiv],
        other_max=other_max,
    )

    # ---- 停復牌 ---------------------------------------------------------------
    today_sus = []
    for ev in suspended_parsed:
        try:
            ad = datetime.strptime(ev.announce_date[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if ad == today:
            today_sus.append(ev)
    w_sus = [ev for ev in today_sus if ev.stock_id in ws]
    o_sus = [ev for ev in today_sus if ev.stock_id not in ws]
    _append_kind_block(
        lines, "停復牌",
        [f"・★ {ev.stock_id} 公告 {ev.announce_date}，復牌 {ev.resumption_date or '—'}" for ev in w_sus],
        [f"・{ev.stock_id} 公告 {ev.announce_date}，復牌 {ev.resumption_date or '—'}" for ev in o_sus],
        other_max=other_max,
    )

    # ---- 注意股 ---------------------------------------------------------------
    all_attn = list(attention or [])
    w_attn, o_attn = _split_watch(all_attn, ws)
    _append_kind_block(
        lines, "注意股",
        [f"・★ {a.stock_id} {a.stock_name}｜{a.note or '異常波動'}" for a in w_attn],
        [f"・{a.stock_id} {a.stock_name}｜{a.note or '異常波動'}" for a in o_attn],
        other_max=other_max,
    )

    # ---- 處置股 ---------------------------------------------------------------
    all_disp = list(disposal or [])
    w_disp, o_disp = _split_watch(all_disp, ws)
    _append_kind_block(
        lines, "處置股",
        [f"・★ {d.stock_id} {d.stock_name}｜{d.measure}｜{d.reason}｜期間 {d.period}" for d in w_disp],
        [f"・{d.stock_id} {d.stock_name}｜{d.measure}｜{d.reason}｜期間 {d.period}" for d in o_disp],
        other_max=other_max,
    )

    # ---- 股東會（已過 watch_set 過濾，全部顯示）-------------------------------
    for m in (sh_meetings or []):
        meet = m.meeting_date.isoformat() if m.meeting_date else "—"
        lines.append(f"・★ 股東會｜{m.stock_id} {m.stock_name}｜{m.meeting_kind} {meet}")
    if sh_meetings:
        lines.append("")

    # ---- 融券回補（已過 watch_set 過濾，全部顯示）------------------------------
    for s in (short_cover or []):
        meet = s.meeting_date.isoformat() if s.meeting_date else "—"
        lines.append(
            f"・★ 融券回補｜{s.stock_id} {s.stock_name}｜停過戶起 "
            f"{s.book_close_start.isoformat() if s.book_close_start else '—'}"
            f"（會 {meet}）"
        )
    if short_cover:
        lines.append("")

    # 移除尾端多餘空行
    while lines and lines[-1] == "":
        lines.pop()

    if len(lines) == initial_len:
        lines.append("（今日 TWSE 預告表無列管／或暫無資料）")
    return "\n".join(lines)




def run_premarket(settings: Settings, *, force: bool = False) -> None:
    if not settings.tw_briefing_enabled:
        logger.info("TW_BRIEFING_ENABLED=false，略過盤前簡報")
        return
    if not settings.finmind_token:
        logger.warning("FINMIND_TOKEN 未設定，略過盤前簡報")
        return

    now = datetime.now(_TZ_TW)
    today = now.date()

    notifier = NotifyHub(settings)
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

    attention_all = fetch_attention_stocks()
    # 處置股先剔除權證／牛熊證等衍生性商品（名稱含「購／售／牛／熊」或非 4 碼數字）
    disposal_all = exclude_warrants_disposal(fetch_disposal_stocks())
    meetings_all = fetch_shareholder_meetings()
    today_attention = attentions_on(attention_all, today)
    today_disposal = disposals_on(disposal_all, today)

    # 核心觀察清單：proxy 個股（僅這些視為「我在意的標的」，顯示時加 ★ 優先）
    watch_set: set = set(settings.tw_market_proxy_stocks)

    # 事件過濾基準（較寬鬆）：加入今日 TWSE 列管個股，用於股東會／融券回補過濾
    event_filter_set: set = set(watch_set)
    for _e in events_on_date(tw48_rows, today):
        event_filter_set.add(_e.stock_id)
    for _a in today_attention:
        event_filter_set.add(_a.stock_id)
    for _d in today_disposal:
        event_filter_set.add(_d.stock_id)

    # 股東會／融券回補：以 event_filter_set 過濾，只顯示觀察清單個股
    today_meetings = [
        m for m in shareholder_meetings_on(meetings_all, today) if m.stock_id in event_filter_set
    ]
    today_short_cover = [
        s for s in short_cover_on(meetings_all, today) if s.stock_id in event_filter_set
    ]

    rss_items = get_digest_rss_items()
    macro_txt = format_macro_from_rss(rss_items)
    tw_hint = format_tw_event_hints_from_rss(rss_items)

    # 重大訊息（MOPS）：watchlist 個股優先，限制總長避免推播被截
    try:
        material_news = fetch_material_news()
        material_news_txt = format_material_news_block(
            material_news,
            watch_tickers=watch_set,
            max_total=30,
            max_watch=20,
            max_other=10,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("抓取 MOPS 重大訊息失敗：%s", e)
        material_news_txt = ""

    idx_bars, idx_id = weighted_index_bars(client, settings.tw_index_stock_id, prev, n=2)
    prev_prev = cal.previous_trading_day(prev)
    prev_close_for_idx: Optional[float] = None
    if len(idx_bars) >= 2:
        prev_close_for_idx = idx_bars[-2].close
    elif prev_prev:
        older = stock_bars(client, idx_id, prev_prev, n=1)
        if older:
            prev_close_for_idx = older[-1].close

    # 用 TWSE FMTQIK 補上一交易日的大盤總成交與漲跌點數（FinMind TAIEX 缺）
    if idx_bars:
        totals = market_totals_on(prev)
        if totals is not None:
            last = idx_bars[-1]
            new_last = replace(
                last,
                trading_money=totals.trade_value_yuan or last.trading_money,
                volume=totals.trade_volume_shares or last.volume,
            )
            idx_bars = idx_bars[:-1] + [new_last]
            if (not prev_close_for_idx) and totals.taiex_close and totals.change_pts is not None:
                prev_close_for_idx = totals.taiex_close - totals.change_pts

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

    title = f"🌅 台股盤前重點（{today.isoformat()}）"
    subtitle = "盤前｜大盤、熱門族群、熱門個股"
    generated_at = utc_now_iso()
    html_path = settings.tw_report_dir / f"morning-{today.isoformat()}.html"

    # 今日／本週／國際 RSS 摘要放 extra_sections（HTML 版可見）
    sus_parsed = [x for x in (parse_suspended_row(r) for r in sus_raw) if x]
    # 今日重點只顯示「今天新公告」的處置股（announce_date == today）。
    # 進行中（處置期間涵蓋今日）的改由 collect_in_progress 的「進行中事件」區塊顯示，
    # 避免同一檔股票在「今日重點」和「進行中事件」兩個段落各出現一次。
    today_disposal_new = [d for d in today_disposal if d.announce_date == today]
    today_events_txt = _format_today_events(
        today,
        tw48_rows,
        sus_parsed,
        attention=today_attention,
        disposal=today_disposal_new,
        sh_meetings=today_meetings,
        short_cover=today_short_cover,
        watch_set=watch_set,
    )
    # 滾動視窗：未來 N 個交易日的預告（股東會/融券回補以 event_filter_set 過濾）
    lookahead_items = collect_lookahead(
        base_date=today,
        cal=cal,
        n_trading_days=settings.tw_events_lookahead_days,
        include_kinds=settings.tw_events_lookahead_kinds,
        tw48_rows=tw48_rows,
        sh_meetings=meetings_all,
        suspended=sus_parsed,
        watch_tickers=event_filter_set,
    )
    lookahead_txt = format_lookahead_block(
        lookahead_items,
        base_date=today,
        max_per_kind_per_day=settings.tw_weekly_events_max_per_day,
        watch_set=watch_set,
    )

    in_progress_items = collect_in_progress(
        today=today,
        disposals=disposal_all,
        sh_meetings=meetings_all,
        include_disposal=("disposal" in settings.tw_events_inprogress_kinds),
        include_book_close=("book_close" in settings.tw_events_inprogress_kinds),
        watch_tickers=event_filter_set,
    )
    in_progress_txt = format_in_progress_block(
        in_progress_items,
        today=today,
        max_per_kind=settings.tw_weekly_events_max_per_day,
        watch_set=watch_set,
    )

    extra_sections = [
        {"title": "今日重點（除權息／停復牌／注意處置／股東會／融券回補）", "body_html": _text_block_to_html(today_events_txt)},
        {"title": f"近 {settings.tw_events_lookahead_days} 日事件預告", "body_html": _text_block_to_html(lookahead_txt)},
        {"title": "進行中事件（期間覆蓋今日）", "body_html": _text_block_to_html(in_progress_txt)},
    ]
    extra_sections.extend(
        [
            {"title": "族群強弱（詳）", "body_html": _text_block_to_html(sector_txt)},
            {"title": "成交活躍（詳）", "body_html": _text_block_to_html(turnover_txt)},
            {"title": "上一交易日大盤敘述", "body_html": _text_block_to_html(f"加權（{idx_id}）：{idx_line_text}")},
            {"title": "隔夜海外（RSS）", "body_html": _text_block_to_html(macro_txt)},
            {"title": "RSS 台股關鍵字提示", "body_html": _text_block_to_html(tw_hint)},
        ]
    )

    image_url: Optional[str] = None

    # ---- v2 dashboard（overview + watchlist）-------------------------------
    overview_path = settings.tw_report_dir / f"morning-overview-{today.isoformat()}.png"
    watchlist_path = settings.tw_report_dir / f"morning-watchlist-{today.isoformat()}.png"
    overview_url: Optional[str] = None
    watchlist_url: Optional[str] = None
    try:
        mb = MarketBreadthClient()
        mi_index_rows = mb.fetch_mi_index()
        twse_shares = mb.fetch_twse_company_info()
        twse_quotes = mb.fetch_twse_quotes(shares_map=twse_shares)
        tpex_quotes = mb.fetch_tpex_quotes()
        # FinMind TPEx K 線：補 MI_INDEX 不含的「櫃買指數」收盤
        # 注意：data_id=TPEx 需付費方案；402 時退回成份股平均 pct，不影響圖生成
        try:
            tpex_bars = client.bars_on_or_before("TPEx", prev, n=2)
            tpex_pair = (
                (tpex_bars[-2].close, tpex_bars[-1].close)
                if len(tpex_bars) >= 2
                else None
            )
        except Exception as _tpex_err:
            logger.warning("TPEx 指數 K 線無法取得（可能需付費方案），退回成份股平均：%s", _tpex_err)
            tpex_pair = None
        indices = build_index_summary(
            mi_index_rows=mi_index_rows,
            twse_quotes=twse_quotes,
            tpex_quotes=tpex_quotes,
            meta_map=meta_map,
            tpex_index_pair=tpex_pair,
        )
        inflow, outflow = build_industry_flow(
            twse_quotes=twse_quotes,
            tpex_quotes=tpex_quotes,
            meta_map=meta_map,
        )
        render_overview_png(
            OverviewData(
                title=title,
                subtitle=f"{prev.isoformat()}（上一交易日）｜全市場指數與產業資金流",
                session_label=str(prev),
                indices=indices,
                inflow=inflow,
                outflow=outflow,
                footer=f"Generated at {generated_at}｜TWSE OpenAPI + FinMind",
            ),
            overview_path,
        )
        render_watchlist_png(
            WatchlistData(
                title="觀察清單熱力 + 強弱族群",
                subtitle=f"{prev.isoformat()}｜{digest.total_members} 檔關注標的",
                digest=digest,
                proxy_stats=proxy_stats,
                footer=f"Generated at {generated_at}",
            ),
            watchlist_path,
        )
        uploader = ImageUploader(settings.imgbb_api_key)
        overview_url = uploader.upload(overview_path)
        watchlist_url = uploader.upload(watchlist_path)
    except Exception as e:
        logger.exception("產生 v2 儀表板（overview/watchlist）失敗：%s", e)

    # HTML dashboard（image_url=None 時 HTML 內嵌本機 PNG 路徑）
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

    # 推送：v2 拆兩張圖（overview + watchlist），LINE 與 Telegram 收到完全相同內容
    pushed = False
    if settings.tw_push_mode == "visual":
        pushed = _push_visual(
            notifier=notifier,
            title=title,
            overview_url=overview_url,
            watchlist_url=watchlist_url,
            material_news_txt=material_news_txt,
            today_events_txt=today_events_txt,
            lookahead_txt=lookahead_txt,
            in_progress_txt=in_progress_txt,
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
            lookahead_txt=lookahead_txt,
            in_progress_txt=in_progress_txt,
            today_events_txt=today_events_txt,
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

    # 注意股 / 處置股：列管當天觀察盤後是否續強或被打回（重點關注）
    for a in today_attention:
        watch.append(a.stock_id)
        ev_records.append(
            BriefingEventRecord(
                kind="attention",
                stock_id=a.stock_id,
                label="注意股",
                ref_date=a.announce_date.isoformat(),
                detail=f"{a.stock_name}｜{a.note or '異常波動'}",
            )
        )
    for d in today_disposal:
        watch.append(d.stock_id)
        ev_records.append(
            BriefingEventRecord(
                kind="disposal",
                stock_id=d.stock_id,
                label=f"處置股｜{d.measure}",
                ref_date=d.announce_date.isoformat(),
                detail=f"{d.stock_name}｜{d.reason}｜期間 {d.period}",
            )
        )

    # 股東會 + 融券強制回補（已用 watch_set 過濾，僅保留觀察清單個股）
    for m in today_meetings:
        watch.append(m.stock_id)
        meet = m.meeting_date.isoformat() if m.meeting_date else ""
        ev_records.append(
            BriefingEventRecord(
                kind="shareholder_meeting",
                stock_id=m.stock_id,
                label=f"{m.meeting_kind}股東會",
                ref_date=meet or today.isoformat(),
                detail=m.stock_name,
            )
        )
    for s in today_short_cover:
        watch.append(s.stock_id)
        ev_records.append(
            BriefingEventRecord(
                kind="short_cover",
                stock_id=s.stock_id,
                label="融券強制回補",
                ref_date=today.isoformat(),
                detail=(
                    f"{s.stock_name}｜停過戶起 "
                    f"{s.book_close_start.isoformat() if s.book_close_start else '—'}"
                ),
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
    notifier: NotifyHub,
    title: str,
    overview_url: Optional[str],
    watchlist_url: Optional[str],
    material_news_txt: str,
    today_events_txt: str,
    lookahead_txt: str,
    in_progress_txt: str,
    macro_txt: str,
    html_path,
    force_banner: bool,
) -> bool:
    """v2：推 2 張公開 URL 圖（指數+雙產業熱力；觀察清單熱力+強弱清單）+ 文字 tail。

    LINE 與 Telegram 收到完全相同內容，不再使用 Flex 卡片。
    若兩張圖都沒上傳成功（imgbb 失效或未設定），回傳 False，由呼叫端走文字 fallback。

    為確保「重大訊息」watchlist 個股不被單則訊息上限截斷，採多則訊息：
      1. 圖①（overview）
      2. 圖②（watchlist）
      3. 重大訊息（watchlist 優先）
      4. 今日重點 + 近期預告 + 進行中事件 + 海外（合併）
    """
    if not overview_url and not watchlist_url:
        logger.warning("v2 dashboard 兩張圖均無法上傳（imgbb 未設定或失敗），改走文字 fallback")
        return False

    ok = True
    if force_banner:
        if not notifier.push_text_chunks(
            f"{title}\n【測試】本日非 FinMind 交易日；大盤／族群以上一交易日收盤為準。"
        ):
            ok = False
    if overview_url:
        if not notifier.push_image(overview_url):
            ok = False
    if watchlist_url:
        if not notifier.push_image(watchlist_url):
            ok = False

    # 圖之後優先送「重大訊息」確保 watchlist 個股不被擠掉
    if material_news_txt:
        if not notifier.push_text_chunks(material_news_txt):
            ok = False

    # tail 文字（今日重點、預告、進行中、海外摘要）合併送
    tail_lines: List[str] = []
    if today_events_txt:
        tail_lines.append(today_events_txt)
    if lookahead_txt:
        tail_lines.append(lookahead_txt)
    if in_progress_txt:
        tail_lines.append(in_progress_txt)
    if macro_txt:
        tail_lines.append(macro_txt)
    tail_lines.append(f"📎 本機儀表板：{html_path}")
    tail_text = "\n\n".join([t for t in tail_lines if t])
    if tail_text:
        if not notifier.push_text_chunks(tail_text):
            ok = False

    return ok


def _build_text_fallback(
    *,
    force: bool,
    is_non_trading: bool,
    today: date,
    title: str,
    generated_at: str,
    macro_txt: str,
    tw_hint: str,
    lookahead_txt: str,
    in_progress_txt: str,
    today_events_txt: str,
) -> str:
    parts: List[str] = []
    if force and is_non_trading:
        parts.extend(
            [
                "【測試】本日非 FinMind 交易日曆之交易日，仍產製簡報。",
                "大盤指數請直接看圖片。",
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
    parts.append(today_events_txt)
    parts.append("")
    if lookahead_txt:
        parts.append(lookahead_txt)
        parts.append("")
    if in_progress_txt:
        parts.append(in_progress_txt)
    return "\n".join(parts).rstrip()
