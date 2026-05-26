"""共用 HTTP client：requests.Session + 預設 timeout / log / 可選重試。

語意分層：
    底層 ``HttpClient`` 只負責 transport（Session、timeout、headers、retry、log）。
    它**不**主動 ``raise_for_status()`` 也**不**吞 exception；要不要把 4xx/5xx
    視為錯誤、要不要降級回空清單，**全部由呼叫端自己決定**——這樣才能保留各
    模組原有的錯誤策略（FinMind 拋錯、TWSE 回 []、LINE 看 status_code 200）。

設計重點：
    1. 內部維護 ``requests.Session()``，所有呼叫共用連線池
    2. 預設 retry total=0（與現狀一致），預留欄位給未來擴充
    3. log 統一加 ``name`` 便於追蹤（finmind / twse / line / imgbb / rss / cnyes）
    4. 每個方法都接受 ``timeout=`` per-call override（因為 FinMind 用 90/120）
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ChunkedEncodingError, ConnectionError as ReqConnError

try:
    # urllib3 >= 1.26
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - fallback
    Retry = None  # type: ignore[assignment]

from financial_news.core.utils import setup_logger

logger = setup_logger(__name__)


@dataclass(frozen=True)
class HttpRetryPolicy:
    """連線層重試政策（透過 urllib3 Retry 掛在 Session adapter）。"""

    total: int = 0  # 0 = 不重試（與現狀一致）
    backoff_factor: float = 0.5
    status_forcelist: tuple = (502, 503, 504)
    allowed_methods: tuple = ("GET", "POST")


@dataclass
class HttpClient:
    """薄包裝：Session + 預設參數 + log。

    使用方式：
        client = HttpClient(timeout=60, name="twse")
        data = client.get_json(url)              # 失敗會拋 RequestException
        resp = client.post_json(url, json=...)   # 回原始 Response，呼叫端自己看 status
    """

    timeout: float = 60.0
    default_headers: Optional[dict] = None
    retry: Optional[HttpRetryPolicy] = None
    name: str = "http"
    _session: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        sess = requests.Session()
        if self.default_headers:
            sess.headers.update(self.default_headers)
        if self.retry and self.retry.total > 0 and Retry is not None:
            r = Retry(
                total=self.retry.total,
                backoff_factor=self.retry.backoff_factor,
                status_forcelist=list(self.retry.status_forcelist),
                allowed_methods=list(self.retry.allowed_methods),
            )
            adapter = HTTPAdapter(max_retries=r)
            sess.mount("http://", adapter)
            sess.mount("https://", adapter)
        self._session = sess

    # ---- helpers ------------------------------------------------------------

    def _to(self, override: Optional[float]) -> float:
        return override if override is not None else self.timeout

    # ---- public API ---------------------------------------------------------

    def get_json(
        self,
        url: str,
        *,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
        tries: int = 1,
        retry_backoff: float = 1.5,
    ) -> Any:
        """GET → ``raise_for_status`` → ``.json()``；失敗會把例外往外拋。

        當 ``tries > 1`` 時，遇到下列暫時性錯誤會在 ``retry_backoff`` 秒後重試：
          - ``json.JSONDecodeError``（含 ``requests.JSONDecodeError``）：上游回傳截斷的 JSON
          - ``ChunkedEncodingError`` / ``ConnectionError`` / ``Timeout``：傳輸層中斷

        TWSE / TPEX OpenAPI 大檔（~3MB）偶爾會被代理切斷，1–2 次重試後通常會回完整內容。
        """
        attempts = max(1, tries)
        last_exc: Optional[BaseException] = None
        for i in range(attempts):
            try:
                resp = self._session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self._to(timeout),
                )
                resp.raise_for_status()
                return resp.json()
            except (
                requests.exceptions.JSONDecodeError,
                ChunkedEncodingError,
                ReqConnError,
                requests.exceptions.Timeout,
            ) as e:
                last_exc = e
                if i + 1 < attempts:
                    logger.warning(
                        "[%s] get_json transient error (try %d/%d) %s: %s",
                        self.name, i + 1, attempts, type(e).__name__, str(e)[:200],
                    )
                    _time.sleep(retry_backoff * (i + 1))
                    continue
                raise
        assert last_exc is not None
        raise last_exc  # pragma: no cover

    def get_bytes(
        self,
        url: str,
        *,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> bytes:
        """GET → ``raise_for_status`` → ``.content``（給 RSS/feedparser 用）。"""
        resp = self._session.get(
            url,
            headers=headers,
            timeout=self._to(timeout),
        )
        resp.raise_for_status()
        return resp.content

    def get_text(
        self,
        url: str,
        *,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        encoding: str = "utf-8",
        timeout: Optional[float] = None,
    ) -> str:
        """GET → ``raise_for_status`` → ``.text``；強制指定 ``encoding`` 避免 mojibake。"""
        resp = self._session.get(
            url,
            params=params,
            headers=headers,
            timeout=self._to(timeout),
        )
        resp.raise_for_status()
        resp.encoding = encoding
        return resp.text

    def post_json(
        self,
        url: str,
        *,
        json: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> requests.Response:
        """POST JSON；**不**做 ``raise_for_status``，由呼叫端決定。"""
        return self._session.post(
            url,
            json=json,
            headers=headers,
            timeout=self._to(timeout),
        )

    def post_multipart(
        self,
        url: str,
        *,
        files: Optional[dict] = None,
        data: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> requests.Response:
        """POST multipart（給 imgbb 上傳用）；**不**做 ``raise_for_status``。"""
        return self._session.post(
            url,
            files=files,
            data=data,
            headers=headers,
            timeout=self._to(timeout),
        )

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass
