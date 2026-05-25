"""TAIFEX 個股期貨保證金 fetcher + 每日快照 diff。

TAIFEX 公開頁面 ``stockMargining`` 顯示「目前」個股期貨保證金級距，
但**沒有**單獨「保證金調整公告」開放資料端點。本模組策略：

    1. 每日盤後抓 :data:`TAIFEX_STOCK_MARGINING_URL` HTML 並解析表格。
    2. 將「商品代碼、商品名稱、股票代號、結算 / 維持 / 原始保證金比例」存成快照。
    3. 與上一個交易日快照比對 → 列出**有變動**的商品（調整前 → 調整後）。

快照檔路徑：``<tw_state_dir>/taifex/margin-YYYY-MM-DD.json``。

欄位範例（取自實際 HTML）：

    | 編號 | 商品代號 | 股票代號 | 商品中文名稱 | 公司全名 | 級距 | 結算 | 維持 | 原始 |
    | 1   | DFF      | 1101    | 台泥期貨    | 臺灣水泥 | 級距1 | 10.00% | 10.35% | 13.50% |
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from financial_news.core.api_endpoints import TAIFEX_STOCK_MARGINING_URL
from financial_news.core.http import HttpClient
from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)


@dataclass(frozen=True)
class MarginRate:
    """單一商品的保證金比例（清算 / 維持 / 原始）。"""

    product_code: str  # 如 "DFF"
    stock_id: str  # 標的股票代號 "1101"
    product_name: str  # 如 "台泥期貨"
    tier: str  # "級距1" / "級距2" / "級距3"
    settlement_pct: float  # 結算保證金比例（%）
    maintenance_pct: float  # 維持保證金比例（%）
    initial_pct: float  # 原始保證金比例（%）


@dataclass(frozen=True)
class MarginChange:
    """單一商品的保證金變動（前 vs 今）。"""

    product_code: str
    stock_id: str
    product_name: str
    old_initial: Optional[float]  # 調整前原始保證金比例
    new_initial: float  # 調整後原始保證金比例
    old_maintenance: Optional[float] = None
    new_maintenance: float = 0.0
    old_settlement: Optional[float] = None
    new_settlement: float = 0.0
    old_tier: Optional[str] = None
    new_tier: str = ""


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def _to_pct(s: str) -> float:
    """``"13.50%"`` → ``13.5`` ；無法解析回 ``0.0``。"""
    s = (s or "").strip().rstrip("%").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


_ROW_RE = re.compile(
    r"<tr>\s*"
    r"<td[^>]*headers=\"id_[ab]\"[^>]*>\s*\d+\s*</td>\s*"
    r"<td[^>]*headers=\"bond_id_[ab]\"[^>]*>\s*([A-Z0-9]+)\s*</td>\s*"
    r"<td[^>]*headers=\"commodity_stock_id_[ab]\"[^>]*>\s*([^\s<]+)\s*</td>\s*"
    r"<td[^>]*headers=\"bond_ch_name1_[ab]\"[^>]*>\s*([^<]+?)\s*</td>\s*"
    r"<td[^>]*headers=\"bond_ch_name2_[ab]\"[^>]*>([^<]+)</td>\s*"
    r"<td[^>]*headers=\"bond_cate_[ab]\"[^>]*>\s*([^<]+?)\s*</td>\s*"
    r"<td[^>]*headers=\"bond_rate1\"[^>]*>\s*([0-9.]+%)\s*</td>\s*"
    r"<td[^>]*headers=\"bond_rate2\"[^>]*>\s*([0-9.]+%)\s*</td>\s*"
    r"<td[^>]*headers=\"bond_rate3\"[^>]*>\s*([0-9.]+%)\s*</td>\s*"
    r"</tr>",
    re.DOTALL,
)


def fetch_stock_margining(*, http: Optional[HttpClient] = None) -> List[MarginRate]:
    """抓 TAIFEX 個股期貨保證金頁面，解析表格→ ``List[MarginRate]``。

    解析失敗或網路錯誤皆回空清單並 log。
    """
    client = http or HttpClient(timeout=30.0, name="taifex_margin")
    try:
        resp = client.get_text(TAIFEX_STOCK_MARGINING_URL)
    except Exception as e:  # noqa: BLE001
        logger.warning("TAIFEX stockMargining 抓取失敗：%s", e)
        return []

    rates: List[MarginRate] = []
    for m in _ROW_RE.finditer(resp):
        product_code, stock_id, name, _full_name, tier, r_set, r_main, r_init = m.groups()
        rates.append(
            MarginRate(
                product_code=product_code.strip(),
                stock_id=stock_id.strip(),
                product_name=name.strip(),
                tier=tier.strip(),
                settlement_pct=_to_pct(r_set),
                maintenance_pct=_to_pct(r_main),
                initial_pct=_to_pct(r_init),
            )
        )
    logger.info("TAIFEX 個股期貨保證金：解析 %d 筆", len(rates))
    return rates


def _snapshot_path(state_dir: Path, d: date) -> Path:
    return state_dir / "taifex" / f"margin-{d.isoformat()}.json"


def save_margin_snapshot(state_dir: Path, d: date, rates: List[MarginRate]) -> Path:
    """寫入今日快照到 ``<state_dir>/taifex/margin-YYYY-MM-DD.json``。"""
    p = _snapshot_path(state_dir, d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps([asdict(r) for r in rates], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return p


def load_margin_snapshot(state_dir: Path, d: date) -> List[MarginRate]:
    """讀取指定日期快照；不存在回空清單。"""
    p = _snapshot_path(state_dir, d)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("讀取 TAIFEX 快照失敗 %s：%s", p, e)
        return []
    return [MarginRate(**row) for row in data]


def diff_margin(prev: List[MarginRate], curr: List[MarginRate]) -> List[MarginChange]:
    """比對前後兩日快照，回傳「原始保證金比例有差異」的商品清單。

    若前一日為空（首次紀錄）則回空清單。
    """
    if not prev or not curr:
        return []

    prev_map: Dict[str, MarginRate] = {r.product_code: r for r in prev}
    out: List[MarginChange] = []
    for r in curr:
        old = prev_map.get(r.product_code)
        if not old:
            continue  # 新上市商品先略過
        if (
            old.initial_pct == r.initial_pct
            and old.maintenance_pct == r.maintenance_pct
            and old.settlement_pct == r.settlement_pct
        ):
            continue
        out.append(
            MarginChange(
                product_code=r.product_code,
                stock_id=r.stock_id,
                product_name=r.product_name,
                old_initial=old.initial_pct,
                new_initial=r.initial_pct,
                old_maintenance=old.maintenance_pct,
                new_maintenance=r.maintenance_pct,
                old_settlement=old.settlement_pct,
                new_settlement=r.settlement_pct,
                old_tier=old.tier,
                new_tier=r.tier,
            )
        )
    return out


def format_margin_block(changes: List[MarginChange], *, max_items: int = 30) -> str:
    """格式化保證金調整段（圖片之後的文字段）。

    欄位：商品、原始保證金比例（前→後）。同時附上維持與結算比例變動以利交叉確認。
    """
    if not changes:
        return ""

    lines = ["【個股期貨保證金調整（vs 上一交易日）】", ""]
    for ch in changes[:max_items]:
        head = f"・{ch.product_code} {ch.product_name}（{ch.stock_id}）"
        # 原始保證金（用戶指定主要欄位）
        if ch.old_initial is not None and ch.old_initial != ch.new_initial:
            head += f"｜原始 {ch.old_initial:.2f}% → {ch.new_initial:.2f}%"
        else:
            head += f"｜原始 {ch.new_initial:.2f}%"
        # 級距變動
        if ch.old_tier and ch.old_tier != ch.new_tier:
            head += f"（{ch.old_tier} → {ch.new_tier}）"
        lines.append(head)
        # 附帶：維持 / 結算（精簡顯示）
        sub_parts = []
        if (
            ch.old_maintenance is not None
            and ch.old_maintenance != ch.new_maintenance
        ):
            sub_parts.append(
                f"維持 {ch.old_maintenance:.2f}%→{ch.new_maintenance:.2f}%"
            )
        if (
            ch.old_settlement is not None
            and ch.old_settlement != ch.new_settlement
        ):
            sub_parts.append(
                f"結算 {ch.old_settlement:.2f}%→{ch.new_settlement:.2f}%"
            )
        if sub_parts:
            lines.append("    " + "、".join(sub_parts))

    if len(changes) > max_items:
        lines.append(f"…另 {len(changes) - max_items} 檔（略）")

    return "\n".join(lines).rstrip()
