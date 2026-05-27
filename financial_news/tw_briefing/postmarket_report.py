"""盤後走勢總結：組資料 → PNG / HTML / Flex；從盤前 state 做事件驗證。"""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from config.settings import Settings
from financial_news.notify_hub import NotifyHub
from financial_news.tw_briefing.briefing_state import (
    BriefingEventRecord,
    BriefingState,
    load_state,
    utc_now_iso,
)
from financial_news.image_uploader import ImageUploader
from financial_news.tw_briefing.dashboard_v2 import (
    OverviewData,
    WatchlistData,
    render_overview_png,
    render_watchlist_png,
)
from financial_news.tw_briefing.event_window import (
    collect_lookahead,
    format_next_day_brief,
)
from financial_news.tw_briefing.exdividend import fetch_twt48u_all
from financial_news.tw_briefing.finmind_client import FinMindClient, IndexBar
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
from financial_news.tw_briefing.market_text import describe_index_session
from financial_news.tw_briefing.mops_material_news import (
    fetch_material_news,
    format_material_news_block,
)
from financial_news.tw_briefing.taifex_margin import (
    diff_margin,
    fetch_stock_margining,
    format_margin_block,
    load_margin_snapshot,
    save_margin_snapshot,
)
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
    client: FinMindClient,  # noqa: ARG001 - 保留簽名相容，已不再抓盤後 K 線
    session: date,  # noqa: ARG001
    state: Optional[BriefingState],
    meta_map: Optional[Dict[str, StockMeta]] = None,
) -> str:
    """簡化版事件列管：列出今日 watchlist 內的事件名單（不再帶當日漲跌結果）。

    盤後個股漲跌可由圖片中的 watchlist 熱力圖直接看出，所以這裡不再呼叫
    FinMind 抓 K 線；改成單純呈現「除權息 / 股東會 / 處置股 / 注意股 / 融券回補 / …」
    名單。重大訊息（material_news）改由 :mod:`mops_material_news` 取得，不再混在此區段。
    """
    lines = ["【事件列管（盤前盤點清單）】", ""]
    if not state or not state.events:
        lines.append("（無盤前 state 或當日無列管事件；若剛啟用模組屬正常）")
        return "\n".join(lines)

    by_kind: Dict[str, List[BriefingEventRecord]] = {}
    for ev in state.events:
        if not ev.stock_id:
            continue
        # material_news 改由 MOPS fetcher 提供完整資訊，不再從 state 撈
        if (ev.kind or "") == "material_news":
            continue
        by_kind.setdefault(ev.kind or "other", []).append(ev)

    kind_order = [
        "earnings",
        "shareholder_meeting",
        "conference",
        "short_cover",
        "exdiv",
        "suspended",
        "attention",
        "disposal",
    ]
    seen_kinds = [k for k in kind_order if k in by_kind] + [
        k for k in by_kind if k not in kind_order
    ]

    if not seen_kinds:
        lines.append("（今日無列管事件）")
        return "\n".join(lines)

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
            lines.append(f"・{label}{extra}")
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
    notifier = NotifyHub(settings)
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

    # 重大訊息（MOPS）：watchlist 個股優先 + 限制總長避免推播被截
    watch_set = set(settings.tw_market_proxy_stocks)
    if state and state.watch_tickers:
        watch_set.update(state.watch_tickers)
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

    # 個股期貨保證金調整：抓今日快照，與上一個交易日 diff
    margin_txt = ""
    try:
        curr_rates = fetch_stock_margining()
        if curr_rates:
            if prev:
                prev_rates = load_margin_snapshot(settings.tw_state_dir, prev)
                changes = diff_margin(prev_rates, curr_rates)
                margin_txt = format_margin_block(changes)
            # 不論今日是否首次紀錄，都存今日快照給下次比對
            save_margin_snapshot(settings.tw_state_dir, today, curr_rates)
    except Exception as e:  # noqa: BLE001
        logger.warning("抓取 TAIFEX 保證金調整失敗：%s", e)

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
    html_path = settings.tw_report_dir / f"postmarket-{today.isoformat()}.html"

    index_bar = idx_bars[-1] if idx_bars else None

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

    overview_path = settings.tw_report_dir / f"postmarket-overview-{today.isoformat()}.png"
    watchlist_path = settings.tw_report_dir / f"postmarket-watchlist-{today.isoformat()}.png"
    overview_url: Optional[str] = None
    watchlist_url: Optional[str] = None
    # TWSE OpenAPI 當日資料：要等 17:00～17:30 後才會切換；若還是上一交易日就
    # 阻塞重試，最多等到 deadline（預設 18:00）。拿不到當日就跳過 v2 圖且發提示，
    # 避免送出穿著今日衣服的昨日資料。
    twse_data_stale = False
    mb = MarketBreadthClient()
    if not (force and is_non_trading):
        if not _wait_for_twse_data(
            mb=mb,
            today=today,
            deadline_hour=settings.tw_postmarket_twse_deadline_hour,
            retry_interval_min=settings.tw_postmarket_twse_retry_interval_min,
        ):
            twse_data_stale = True
    try:
        if twse_data_stale:
            raise RuntimeError("TWSE OpenAPI 當日資料超過 deadline 仍未更新")
        mi_index_rows = mb.fetch_mi_index()
        twse_shares = mb.fetch_twse_company_info()
        twse_quotes = mb.fetch_twse_quotes(shares_map=twse_shares)
        tpex_quotes = mb.fetch_tpex_quotes()
        # FinMind TPEx K 線：補 MI_INDEX 不含的「櫃買指數」收盤
        # 注意：data_id=TPEx 需付費方案；402 時退回成份股平均 pct，不影響圖生成
        try:
            tpex_bars = client.bars_on_or_before("TPEx", today, n=2)
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
                subtitle=f"{today.isoformat()}｜全市場指數與產業資金流",
                session_label=today.isoformat(),
                indices=indices,
                inflow=inflow,
                outflow=outflow,
                footer=f"Generated at {generated_at}｜TWSE OpenAPI + FinMind",
            ),
            overview_path,
        )
        render_watchlist_png(
            WatchlistData(
                title="觀察清單熱力 + 強弱族群（盤後）",
                subtitle=f"{today.isoformat()}｜{digest.total_members} 檔關注標的",
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

    stale_notice = ""
    if twse_data_stale:
        stale_notice = (
            f"⏳ 台股盤後｜TWSE OpenAPI 當日資料尚未公布\n\n"
            f"{today.isoformat()} 的指數／個股 OpenAPI（更新時點約 17:00～17:30）"
            f"截止 {settings.tw_postmarket_twse_deadline_hour:02d}:00 仍指向上一交易日，"
            f"本次盤後大盤／產業熱力圖略過。\n"
            f"事件列管、明日預告、重大訊息、保證金調整仍會推送。"
        )

    pushed = False
    if settings.tw_push_mode == "visual":
        pushed = _push_visual(
            notifier=notifier,
            title=title,
            overview_url=overview_url,
            watchlist_url=watchlist_url,
            material_news_txt=material_news_txt,
            margin_txt=margin_txt,
            next_day_txt=next_day_txt,
            stale_notice=stale_notice,
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
            next_day_txt=next_day_txt,
        )
        if not notifier.push_text_chunks(body):
            logger.error("盤後總結 LINE 發送失敗（文字 fallback）")


def _push_visual(
    *,
    notifier: NotifyHub,
    title: str,
    overview_url: Optional[str],
    watchlist_url: Optional[str],
    material_news_txt: str,
    margin_txt: str,
    next_day_txt: str,
    stale_notice: str,
    html_path,
    force_banner: bool,
) -> bool:
    """v2：推 2 張公開 URL 圖（overview + watchlist）+ 文字 tail。

    LINE 與 Telegram 收到完全相同內容；若兩張圖都沒上傳成功且沒有 stale_notice
    可送，回 False 由呼叫端走文字 fallback。

    文字 tail 送多則訊息以確保「重大訊息」與「期貨保證金調整」不會被單則上限截斷：
      1. 圖①（overview） 2. 圖②（watchlist）— 若 TWSE 資料未更新則改推 stale_notice
      3. 重大訊息（watchlist 個股優先）
      4. 個股期貨保證金調整
      5. 明日預告（合併送；事件列管已由盤前「今日重點」覆蓋，盤後不再重送）
    """
    if not overview_url and not watchlist_url and not stale_notice:
        logger.warning("v2 dashboard 兩張圖均無法上傳且無 stale_notice，改走文字 fallback")
        return False

    ok = True
    if force_banner:
        if not notifier.push_text_chunks(
            f"{title}\n【測試】本日非交易日；若無當日 K 線為正常。"
        ):
            ok = False
    if stale_notice:
        if not notifier.push_text_chunks(stale_notice):
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

    # 接著「個股期貨保證金調整」
    if margin_txt:
        if not notifier.push_text_chunks(margin_txt):
            ok = False

    # 最後明日預告 + 報告連結（事件列管已由盤前「今日重點」覆蓋，盤後不重送）
    tail_lines: List[str] = []
    if next_day_txt:
        tail_lines.append(next_day_txt)
    tail_lines.append(f"📎 本機儀表板：{html_path}")
    tail_text = "\n\n".join([t for t in tail_lines if t])
    if tail_text:
        if not notifier.push_text_chunks(tail_text):
            ok = False

    return ok


def _wait_for_twse_data(
    *,
    mb: MarketBreadthClient,
    today: date,
    deadline_hour: int,
    retry_interval_min: int,
) -> bool:
    """阻塞等待 TWSE OpenAPI 切到當日資料；最晚等到當日 ``deadline_hour:00``。

    14:30 跑盤後時 TWSE MI_INDEX / STOCK_DAY_ALL 仍是上一交易日，
    用 :meth:`MarketBreadthClient.probe_data_date` 探查日期，每 ``retry_interval_min``
    分鐘 retry 一次，直到資料切到當日（回 True）或超過 deadline（回 False）。
    """
    deadline = datetime.combine(
        today, datetime.min.time(), tzinfo=_TZ_TW
    ).replace(hour=max(0, min(23, deadline_hour)))
    interval = max(1, retry_interval_min) * 60

    attempt = 0
    while True:
        attempt += 1
        data_date = mb.probe_data_date()
        if data_date == today:
            if attempt > 1:
                logger.info("TWSE 當日資料已就緒（第 %d 次探查）", attempt)
            return True
        now = datetime.now(_TZ_TW)
        if now >= deadline:
            logger.warning(
                "TWSE 等待逾時：data_date=%s, today=%s, deadline=%s",
                data_date,
                today,
                deadline.isoformat(),
            )
            return False
        remaining_sec = max(0, int((deadline - now).total_seconds()))
        sleep_sec = min(interval, remaining_sec)
        if sleep_sec <= 0:
            return False
        logger.info(
            "TWSE 當日資料尚未公布（探到 %s），第 %d 次 retry 將於 %d 秒後執行（deadline %s）",
            data_date,
            attempt,
            sleep_sec,
            deadline.strftime("%H:%M"),
        )
        time.sleep(sleep_sec)


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
        ]
    )
    if next_day_txt:
        parts.append("")
        parts.append(next_day_txt)
    return "\n".join(parts)
