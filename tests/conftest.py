"""공통 테스트 픽스처."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Metric, ProviderName  # noqa: E402
from tests.helpers import FakeConnector, make_source  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """각 테스트 전후로 설정 캐시를 비운다(로컬 .env 영향 차단)."""
    from app.config import reset_settings

    reset_settings()
    yield
    reset_settings()


@pytest.fixture()
def tmp_settings(tmp_path, monkeypatch):
    """키가 전혀 없는 설정(LLM 미사용 모드)."""
    from app.config import load_settings, reset_settings

    for var in ("NVIDIA_API_KEY", "DART_API_KEY", "ECOS_API_KEY", "KOSIS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KIC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KIC_MAX_RUN_SECONDS", "30")
    reset_settings()
    return load_settings()


@pytest.fixture()
def sample_source_pair():
    """서로 같은 지표를 같은 값으로 말하는 출처 2건(정상 검색 시나리오)."""
    text_a = "한국은행 기준금리는 2025-12 기준 3.25 % 입니다."
    text_b = "한국은행 기준금리는 2025-12 기준 3.25 % 로 유지되었습니다."
    a = make_source(
        "ecos:base_rate",
        title="[ECOS] 한국은행 기준금리 (2025-12)",
        url="https://ecos.bok.or.kr/api/StatisticSearch/sample/json/kr/1/1/722Y001/M/202512/202512/0101000/",
        published_at=datetime(2025, 12, 10).date(),
        raw_text=text_a,
        metrics=[
            Metric(
                label="한국은행 기준금리",
                value=3.25,
                raw_value="3.25",
                unit="%",
                period="2025-12",
                source_id="ecos:base_rate",
                context=text_a,
            )
        ],
    )
    b = make_source(
        "ir:bank:1",
        provider=ProviderName.IR,
        title="한국은행 정책금리 결정 발표",
        url="https://example.org/bok/decision",
        published_at=datetime(2025, 12, 11).date(),
        raw_text=text_b,
    )
    return [a, b]
