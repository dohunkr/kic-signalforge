"""KIC SignalForge - 설정 로딩.

모든 자격 증명(API 키)은 환경변수에서만 읽는다.
코드/프론트엔드/저장소 어디에도 키를 하드코딩하지 않는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# .env 가 있으면 로드한다(저장소에는 .env.example 만 존재).
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class NimSettings:
    """NVIDIA NIM 설정.

    공식 문서(build.nvidia.com/llms.txt, docs.nvidia.com/nim) 기준:
      Base URL : https://integrate.api.nvidia.com/v1
      Auth     : Authorization: Bearer $NVIDIA_API_KEY
    """

    api_key: Optional[str]
    base_url: str
    model: str
    timeout_seconds: float

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def is_official_host(self) -> bool:
        return self.base_url.rstrip("/").endswith("integrate.api.nvidia.com/v1")


@dataclass(frozen=True)
class DartSettings:
    """금융감독원 OpenDART 설정."""

    api_key: Optional[str]
    base_url: str = "https://opendart.fss.or.kr/api"
    timeout_seconds: float = 20.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class EcosSettings:
    """한국은행 ECOS 설정.

    인증키를 URL 경로 세그먼트로 전달하는 방식이며, 공식 샘플 키 'sample' 이 있다.
    """

    api_key: Optional[str]
    base_url: str = "https://ecos.bok.or.kr/api"
    timeout_seconds: float = 20.0

    @property
    def effective_key(self) -> str:
        """실제 호출에 사용할 키. 미설정 시 공식 샘플 키를 사용한다."""
        return self.api_key or "sample"

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def using_sample_key(self) -> bool:
        return not self.api_key


@dataclass(frozen=True)
class KosisSettings:
    """국가데이터처 KOSIS 설정."""

    api_key: Optional[str]
    base_url: str = "https://kosis.kr/openapi"
    timeout_seconds: float = 20.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class PriceSettings:
    """상장사 주가 시계열 설정.

    심볼 해석은 DART 기업코드(stock_code) → 공개 시세 API 검색 순서로 진행한다.
    """

    base_url: str = "https://query1.finance.yahoo.com"
    timeout_seconds: float = 20.0
    enabled: bool = True

    @property
    def configured(self) -> bool:
        return self.enabled


@dataclass(frozen=True)
class AppSettings:
    nim: NimSettings
    dart: DartSettings
    ecos: EcosSettings
    kosis: KosisSettings
    price: PriceSettings
    data_dir: Path
    max_run_seconds: float
    max_retries: int
    stale_after_days: int
    host: str
    port: int
    log_level: str
    secrets: tuple[str, ...] = field(default_factory=tuple, repr=False)

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "signalforge.sqlite3"

    def connector_status(self) -> dict[str, dict]:
        """프론트엔드/헬스체크용 상태. 키 값 자체는 절대 포함하지 않는다."""
        return {
            "nim": {
                "label": "NVIDIA NIM (LLM)",
                "configured": self.nim.configured,
                "mode": "live" if self.nim.configured else "unavailable",
                "note": (
                    "키 미설정: LLM 없이 결정론적(규칙 기반) 분석만 동작합니다."
                    if not self.nim.configured
                    else "키 설정됨"
                ),
                "base_url": self.nim.base_url,
                "model": self.nim.model,
            },
            "dart": {
                "label": "금융감독원 OpenDART",
                "configured": self.dart.configured,
                "mode": "live" if self.dart.configured else "unavailable",
                "note": (
                    "키 미설정: DART 자료 수집을 건너뜁니다."
                    if not self.dart.configured
                    else "키 설정됨"
                ),
            },
            "ecos": {
                "label": "한국은행 ECOS",
                "configured": self.ecos.configured,
                "mode": "live" if self.ecos.configured else "sample",
                "note": (
                    "공식 샘플 키('sample')로 동작합니다. 일부 통계만 제공됩니다."
                    if self.ecos.using_sample_key
                    else "키 설정됨"
                ),
            },
            "kosis": {
                "label": "국가데이터처 KOSIS",
                "configured": self.kosis.configured,
                "mode": "live" if self.kosis.configured else "unavailable",
                "note": (
                    "키 미설정: KOSIS 자료 수집을 건너뜁니다."
                    if not self.kosis.configured
                    else "키 설정됨"
                ),
            },
            "ir": {
                "label": "기업 공식 IR (공개 웹페이지)",
                "configured": True,
                "mode": "live",
                "note": "키 불필요. 사용자가 제공한 공개 IR URL만 조회합니다.",
            },
        }


def _collect_secrets() -> tuple[str, ...]:
    """로그 마스킹에 사용할 secret 값 목록(값이 비어 있으면 제외)."""
    out = []
    for name in ("NVIDIA_API_KEY", "DART_API_KEY", "ECOS_API_KEY", "KOSIS_API_KEY"):
        v = (os.environ.get(name) or "").strip()
        if len(v) >= 8:
            out.append(v)
    return tuple(out)


def load_settings() -> AppSettings:
    # Vercel 요청은 함수 실행 시간 안에 응답해야 하므로 느린 AI 재시도를 제한한다.
    is_vercel = _env("VERCEL") == "1"
    nim_timeout = _env_float("NVIDIA_TIMEOUT_SECONDS", 12.0 if is_vercel else 45.0)
    max_retries = _env_int("KIC_MAX_RETRIES", 0 if is_vercel else 2)
    max_run_seconds = _env_float("KIC_MAX_RUN_SECONDS", 60.0 if is_vercel else 90.0)
    if is_vercel:
        nim_timeout = min(nim_timeout, 12.0)
        max_retries = 0
        max_run_seconds = min(max_run_seconds, 60.0)

    nim_key = _env("NVIDIA_API_KEY") or None
    nim_model = _env("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
    # 초대형 기본 모델은 탐색/응답 지연이 길 수 있어 경량 무료 endpoint 를 기본으로 둔다.
    if nim_model == "nvidia/nemotron-3-ultra-550b-a55b":
        nim_model = "nvidia/nemotron-3.5-lightning-30b-a3b"
    dart_key = _env("DART_API_KEY") or None
    ecos_key = _env("ECOS_API_KEY") or None
    kosis_key = _env("KOSIS_API_KEY") or None

    data_dir = Path(_env("KIC_DATA_DIR", "./data"))
    if not data_dir.is_absolute():
        data_dir = (PROJECT_ROOT / data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    return AppSettings(
        nim=NimSettings(
            api_key=nim_key,
            base_url=_env("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            model=nim_model,
            timeout_seconds=nim_timeout,
        ),
        dart=DartSettings(api_key=dart_key),
        ecos=EcosSettings(api_key=ecos_key),
        kosis=KosisSettings(api_key=kosis_key),
        price=PriceSettings(
            base_url=_env("PRICE_API_BASE", "https://query1.finance.yahoo.com"),
            timeout_seconds=_env_float("PRICE_TIMEOUT_SECONDS", 20.0),
            enabled=_env("PRICE_ENABLED", "1") not in ("0", "false", "False"),
        ),
        data_dir=data_dir,
        max_run_seconds=max_run_seconds,
        max_retries=max_retries,
        stale_after_days=_env_int("KIC_STALE_AFTER_DAYS", 400),
        host=_env("HOST", "127.0.0.1"),
        port=_env_int("PORT", 8000),
        log_level=_env("KIC_LOG_LEVEL", "INFO").upper(),
        secrets=_collect_secrets(),
    )


_SETTINGS: Optional[AppSettings] = None


def get_settings() -> AppSettings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


def reset_settings() -> None:
    """테스트에서 환경변수 변경을 반영하기 위해 사용."""
    global _SETTINGS
    _SETTINGS = None
