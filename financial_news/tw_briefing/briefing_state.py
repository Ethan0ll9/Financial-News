"""盤前狀態 JSON：供盤後「事件驗證」讀取。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional


@dataclass
class BriefingEventRecord:
    kind: str
    stock_id: str
    label: str
    ref_date: str
    detail: str = ""


@dataclass
class BriefingState:
    version: int = 1
    session_date: str = ""
    generated_at: str = ""
    index_stock_id: str = ""
    watch_tickers: List[str] = field(default_factory=list)
    events: List[BriefingEventRecord] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "session_date": self.session_date,
            "generated_at": self.generated_at,
            "index_stock_id": self.index_stock_id,
            "watch_tickers": list(self.watch_tickers),
            "events": [asdict(e) for e in self.events],
        }

    @classmethod
    def from_json_dict(cls, d: dict[str, Any]) -> "BriefingState":
        evs: List[BriefingEventRecord] = []
        for row in d.get("events") or []:
            if not isinstance(row, dict):
                continue
            evs.append(
                BriefingEventRecord(
                    kind=str(row.get("kind", "")),
                    stock_id=str(row.get("stock_id", "")),
                    label=str(row.get("label", "")),
                    ref_date=str(row.get("ref_date", "")),
                    detail=str(row.get("detail", "")),
                )
            )
        return cls(
            version=int(d.get("version", 1)),
            session_date=str(d.get("session_date", "")),
            generated_at=str(d.get("generated_at", "")),
            index_stock_id=str(d.get("index_stock_id", "")),
            watch_tickers=[str(x) for x in (d.get("watch_tickers") or [])],
            events=evs,
        )


def state_path_for_session(state_dir: Path, session_date: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{session_date}.json"


def save_state(state_dir: Path, state: BriefingState) -> Path:
    path = state_path_for_session(state_dir, state.session_date)
    path.write_text(
        json.dumps(state.to_json_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_state(state_dir: Path, session_date: str) -> Optional[BriefingState]:
    path = state_path_for_session(state_dir, session_date)
    if not path.is_file():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return BriefingState.from_json_dict(d)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


def utc_now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ", timespec="seconds")
