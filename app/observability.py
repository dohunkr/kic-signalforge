"""관측(observability) - API 키/민감정보를 기록하지 않는 로깅.

규칙
  * API 키, 인증헤더, 쿼리파라미터의 키 값은 로그에 절대 남기지 않는다.
  * 기록하는 것: 커넥터/엔드포인트 종류, 성공/실패, 재시도, 소요시간, 사용 출처 개수.
  * 기록하지 않는 것: API 키, 사용자 개인정보, LLM 프롬프트 원문 전체.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, Optional

from .config import get_settings

_LOG_CONFIGURED = False

#: 로그에서 항상 마스킹할 패턴 (키 형태 문자열)
_PATTERNS = [
    re.compile(r"(crtfc_key|apiKey|api_key|Authorization|authorization)\s*[=:]\s*['\"]?[^&'\s,}\"]{4,}", re.I),
    re.compile(r"(Bearer)\s+[A-Za-z0-9._\-]{8,}", re.I),
    re.compile(r"\b[A-Za-z0-9]{40}\b"),  # DART 인증키(40자리)
]


class SecretRedactingFilter(logging.Filter):
    """로그 레코드에 포함된 민감값을 마스킹한다."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        redacted = self.redact(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True

    def redact(self, text: str) -> str:
        out = text
        for secret in self._secrets:
            if secret in out:
                out = out.replace(secret, "***REDACTED***")
        for pat in _PATTERNS:
            out = pat.sub(lambda m: _mask_match(m), out)
        return out


def _mask_match(match: "re.Match[str]") -> str:
    text = match.group(0)
    if "=" in text or ":" in text:
        head = text.split("=")[0].split(":")[0]
        return f"{head}=***REDACTED***"
    return "***REDACTED***"


def setup_logging(level: Optional[str] = None) -> None:
    """로깅을 구성한다. 중복 구성은 방지한다."""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    settings = get_settings()
    resolved = (level or settings.log_level or "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S")
    )
    handler.addFilter(SecretRedactingFilter(settings.secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, resolved, logging.INFO))
    # httpx 는 매 요청마다 INFO 로그를 남기므로 끈다(우리 로거가 관측 정보를 대신 기록).
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _LOG_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


class RequestTrace:
    """단일 HTTP 호출에 대한 관측 레코드."""

    __slots__ = ("provider", "endpoint", "ok", "status", "duration_ms", "attempt", "error_code")

    def __init__(self, provider: str, endpoint: str) -> None:
        self.provider = provider
        self.endpoint = endpoint
        self.ok = False
        self.status: Optional[int] = None
        self.duration_ms: float = 0.0
        self.attempt: int = 1
        self.error_code: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "ok": self.ok,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "attempt": self.attempt,
            "error_code": self.error_code,
        }


class Observability:
    """호출 통계 수집기. 메모리에만 보관하고 외부로 내보내지 않는다."""

    def __init__(self) -> None:
        self.traces: list[RequestTrace] = []
        self.retries_used = 0
        self.http_calls = 0

    @contextmanager
    def track(self, provider: str, endpoint: str) -> Iterator[RequestTrace]:
        trace = RequestTrace(provider, endpoint)
        started = time.perf_counter()
        try:
            yield trace
        finally:
            trace.duration_ms = (time.perf_counter() - started) * 1000
            self.traces.append(trace)
            self.http_calls += 1

    def summary(self) -> Dict[str, Any]:
        by_provider: Dict[str, Dict[str, Any]] = {}
        for t in self.traces:
            slot = by_provider.setdefault(
                t.provider, {"calls": 0, "ok": 0, "failed": 0, "total_ms": 0.0, "errors": {}}
            )
            slot["calls"] += 1
            slot["total_ms"] += t.duration_ms
            if t.ok:
                slot["ok"] += 1
            else:
                slot["failed"] += 1
                code = t.error_code or ERR_UNLABELED
                slot["errors"][code] = slot["errors"].get(code, 0) + 1
        for slot in by_provider.values():
            slot["total_ms"] = round(slot["total_ms"], 1)
        return {
            "http_calls": self.http_calls,
            "retries_used": self.retries_used,
            "by_provider": by_provider,
        }

    def as_json(self) -> str:
        return json.dumps(self.summary(), ensure_ascii=False)


ERR_UNLABELED = "unlabeled"


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """임의 문자열에서 시크릿을 제거. 에러 메시지 저장 전에 사용."""
    flt = SecretRedactingFilter(secrets)
    return flt.redact(text)
