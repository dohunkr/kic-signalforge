"""공통 HTTP 클라이언트.

재시도/타임아웃/오류분류를 한 곳에 모으고, 로그에 키가 남지 않게 한다.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Mapping, Optional, Tuple

import httpx

from ..errors import (
    ConnectorFailure,
    ERR_NETWORK,
    ERR_SERVER,
    ERR_TIMEOUT,
    map_http_status,
)
from ..observability import Observability, get_logger

log = get_logger(__name__)

DEFAULT_USER_AGENT = (
    "KIC-SignalForge/0.1 (financial research assistant; contact: repository owner)"
)


class HttpClient:
    """재시도와 관측을 갖춘 얇은 HTTP 래퍼."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_retries: int = 2,
        observability: Optional[Observability] = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.obs = observability or Observability()
        self.user_agent = user_agent

    def request(
        self,
        method: str,
        url: str,
        *,
        provider: str,
        endpoint: str,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        json_body: Optional[Any] = None,
        expect_json: bool = True,
        encoding: Optional[str] = None,
        binary: bool = False,
    ) -> Tuple[int, Any]:
        """요청을 수행하고 (status, body) 를 반환한다.

        body 는 기본적으로 JSON(dict) 또는 텍스트(str) 이며,
        binary=True 이면 원시 바이트(bytes) 를 반환한다.
        실패하면 ``ConnectorFailure`` 예외를 던진다.
        """
        merged_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            merged_headers.update(headers)

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 2):
            with self.obs.track(provider, endpoint) as trace:
                trace.attempt = attempt
                try:
                    with httpx.Client(
                        timeout=self.timeout, follow_redirects=True, verify=True
                    ) as client:
                        resp = client.request(
                            method,
                            url,
                            params=params,
                            headers=merged_headers,
                            json=json_body,
                        )
                    trace.status = resp.status_code
                    if resp.status_code == 200:
                        trace.ok = True
                        if binary:
                            return resp.status_code, resp.content
                        if expect_json:
                            try:
                                return resp.status_code, resp.json()
                            except Exception as exc:  # noqa: BLE001
                                trace.error_code = "parse"
                                raise ConnectorFailure(
                                    "parse",
                                    "응답을 JSON으로 해석하지 못했습니다.",
                                    provider=provider,
                                    detail=type(exc).__name__,
                                ) from exc
                        text = resp.content.decode(encoding or "utf-8", errors="replace")
                        return resp.status_code, text

                    code, message = map_http_status(resp.status_code, provider)
                    trace.error_code = code
                    if code in (ERR_SERVER, ERR_TIMEOUT) and attempt <= self.max_retries:
                        self.obs.retries_used += 1
                        log.warning(
                            "%s %s 실패(HTTP %s). 재시도 %s/%s",
                            provider,
                            endpoint,
                            resp.status_code,
                            attempt,
                            self.max_retries,
                        )
                        time.sleep(min(2 ** attempt * 0.4, 3.0))
                        continue
                    raise ConnectorFailure(
                        code,
                        message,
                        provider=provider,
                        detail=f"HTTP {resp.status_code}",
                    )
                except ConnectorFailure:
                    raise
                except httpx.TimeoutException as exc:
                    trace.error_code = ERR_TIMEOUT
                    if attempt <= self.max_retries:
                        self.obs.retries_used += 1
                        log.warning(
                            "%s %s 타임아웃. 재시도 %s/%s",
                            provider,
                            endpoint,
                            attempt,
                            self.max_retries,
                        )
                        continue
                    raise ConnectorFailure(
                        ERR_TIMEOUT,
                        "요청 시간이 초과되었습니다.",
                        provider=provider,
                        detail=type(exc).__name__,
                    ) from exc
                except httpx.HTTPError as exc:
                    last_exc = exc
                    trace.error_code = ERR_NETWORK
                    if attempt <= self.max_retries:
                        self.obs.retries_used += 1
                        time.sleep(min(2 ** attempt * 0.4, 3.0))
                        continue
                    raise ConnectorFailure(
                        ERR_NETWORK,
                        "네트워크 연결에 실패했습니다.",
                        provider=provider,
                        detail=type(exc).__name__,
                    ) from exc
        raise ConnectorFailure(
            ERR_NETWORK,
            "요청이 최종 실패했습니다.",
            provider=provider,
            detail=type(last_exc).__name__ if last_exc else None,
        )

    def get_json(self, url: str, **kwargs: Any) -> Tuple[int, Any]:
        return self.request("GET", url, expect_json=True, **kwargs)

    def get_text(self, url: str, **kwargs: Any) -> Tuple[int, str]:
        return self.request("GET", url, expect_json=False, **kwargs)

    def get_bytes(self, url: str, **kwargs: Any) -> Tuple[int, bytes]:
        """원시 바이트를 받는다(ZIP 등 바이너리 응답용)."""
        kwargs.pop("binary", None)
        return self.request("GET", url, expect_json=False, binary=True, **kwargs)
