"""평가용 시나리오 7종을 그대로 검증한다.

1. 정상 검색   : 최소 2개 출처가 존재할 때 각 핵심 주장에 출처가 표시되는가
2. 출처 없음   : 검색 결과가 없을 때 허위 근거를 만들지 않는가
3. 상충 자료   : 다른 수치/설명이 있을 때 양쪽을 보여주는가
4. API 실패    : 타임아웃/쿼터 오류가 사용자에게 투명하게 표시되는가
5. 최신성      : 오래된 문서가 최신인 것처럼 표시되지 않는가
6. 보안        : API Key가 클라이언트 번들/로그/Git에 포함되지 않는가
7. 샘플 모드   : 샘플 결과가 실제 데이터와 명확히 구별되는가
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.models import (  # noqa: E402
    ConnectorError,
    Metric,
    ProviderName,
    Provenance,
    ResearchRequest,
    RunStatus,
)
from app.pipeline.orchestrator import Orchestrator  # noqa: E402
from tests.helpers import FakeConnector, make_source  # noqa: E402

#: ECOS(금리)와 KOSIS(시장) 두 커넥터가 모두 동작하는 질문
QUESTION = "한국은행 기준금리와 반도체 시장 outlook 영향은?"
REQUEST = ResearchRequest(question=QUESTION, period="2025")


def _orchestrator(settings, connectors):
    return Orchestrator(settings, connectors=connectors)


def _metric(label, value, unit, period, source_id, context):
    return Metric(
        label=label,
        value=value,
        raw_value=str(value),
        unit=unit,
        period=period,
        source_id=source_id,
        context=context,
    )


# --- 1) 정상 검색 ---------------------------------------------------------
def test_scenario1_normal_search_each_claim_has_source(sample_source_pair, tmp_settings):
    ecos, ir = sample_source_pair
    brief = _orchestrator(
        tmp_settings,
        {
            ProviderName.ECOS: FakeConnector(ProviderName.ECOS, sources=[ecos]),
            ProviderName.KOSIS: FakeConnector(ProviderName.KOSIS, sources=[ir]),
        },
    ).run(REQUEST)

    assert brief.status in (RunStatus.COMPLETED, RunStatus.PARTIAL)
    assert len(brief.sources) >= 2
    supported = [c for c in brief.claims if c.status.value == "supported"]
    assert supported, "근거가 있는 주장이 하나도 없습니다."
    for claim in brief.claims:
        assert claim.supporting_source_ids or claim.contradicting_source_ids, (
            "주장에 출처가 하나도 연결되지 않았습니다."
        )
    source_ids = {s.source_id for s in brief.sources}
    for claim in brief.claims:
        for sid in claim.supporting_source_ids + claim.contradicting_source_ids:
            assert sid in source_ids
    assert "https://example.org/bok/decision" in brief.report_markdown


# --- 2) 출처 없음 ---------------------------------------------------------
def test_scenario2_no_sources_creates_no_fabricated_evidence(tmp_settings):
    brief = _orchestrator(
        tmp_settings,
        {
            ProviderName.ECOS: FakeConnector(ProviderName.ECOS, sources=[]),
            ProviderName.DART: FakeConnector(ProviderName.DART, sources=[]),
            ProviderName.KOSIS: FakeConnector(ProviderName.KOSIS, sources=[]),
        },
    ).run(REQUEST)

    assert brief.status is RunStatus.INSUFFICIENT_EVIDENCE
    assert brief.claims == [], "출처가 없는데 주장이 생성되었습니다(허위 근거)."
    assert "확인 불가" in brief.report_markdown or "근거 부족" in brief.report_markdown
    assert brief.data_gaps


# --- 3) 상충 자료 ---------------------------------------------------------
def test_scenario3_conflicting_sources_show_both_sides(tmp_settings):
    text_a = "한국은행 기준금리는 2025-12 기준 3.25 % 입니다."
    text_b = "한국은행 기준금리는 2025-12 기준 3.50 % 라고 보도되었습니다."
    a = make_source(
        "ecos:base_rate",
        title="[ECOS] 한국은행 기준금리 (2025-12)",
        raw_text=text_a,
        published_at=datetime(2025, 12, 10).date(),
        metrics=[_metric("한국은행 기준금리", 3.25, "%", "2025-12", "ecos:base_rate", text_a)],
    )
    b = make_source(
        "kosis:market:1",
        provider=ProviderName.KOSIS,
        title="다른 출처의 시장 통계",
        url="https://example.org/news/1",
        raw_text=text_b,
        published_at=datetime(2025, 12, 12).date(),
        metrics=[_metric("한국은행 기준금리", 3.50, "%", "2025-12", "kosis:market:1", text_b)],
    )

    brief = _orchestrator(
        tmp_settings,
        {
            ProviderName.ECOS: FakeConnector(ProviderName.ECOS, sources=[a]),
            ProviderName.KOSIS: FakeConnector(ProviderName.KOSIS, sources=[b]),
        },
    ).run(REQUEST)

    contested = [c for c in brief.claims if c.status.value == "contested"]
    assert contested, "상이한 값이 있는데 contested 주장이 생성되지 않았습니다."
    claim = contested[0]
    assert claim.supporting_source_ids and claim.contradicting_source_ids
    assert set(claim.supporting_source_ids) & set(claim.contradicting_source_ids) == set()
    assert "3.25" in brief.report_markdown and "3.50" in brief.report_markdown
    assert "자료 상충" in brief.report_markdown or "상이" in brief.report_markdown


# --- 4) API 실패(타임아웃/쿼터) -------------------------------------------
def test_scenario4_api_failures_are_transparent(tmp_settings):
    brief = _orchestrator(
        tmp_settings,
        {
            ProviderName.ECOS: FakeConnector(
                ProviderName.ECOS,
                errors=[
                    ConnectorError(
                        provider=ProviderName.ECOS,
                        code="timeout",
                        message="요청 시간이 초과되었습니다.",
                    )
                ],
            ),
            ProviderName.KOSIS: FakeConnector(
                ProviderName.KOSIS,
                errors=[
                    ConnectorError(
                        provider=ProviderName.KOSIS,
                        code="quota",
                        message="호출 제한을 초과하였습니다.",
                    )
                ],
            ),
        },
    ).run(REQUEST)

    codes = {e.code for e in brief.errors}
    assert "timeout" in codes and "quota" in codes
    assert brief.status is RunStatus.INSUFFICIENT_EVIDENCE
    md = brief.report_markdown
    assert "timeout" in md and "quota" in md
    assert "요청 시간이 초과되었습니다." in md
    assert "호출 제한을 초과하였습니다." in md


def test_scenario4_connector_raises_exception_is_contained(tmp_settings):
    brief = _orchestrator(
        tmp_settings,
        {
            ProviderName.ECOS: FakeConnector(
                ProviderName.ECOS, raises=RuntimeError("boom")
            ),
            ProviderName.KOSIS: FakeConnector(
                ProviderName.KOSIS,
                errors=[
                    ConnectorError(
                        provider=ProviderName.KOSIS, code="auth", message="키가 없습니다."
                    )
                ],
            ),
        },
    ).run(REQUEST)
    assert brief.errors
    assert any(e.code in ("unknown", "auth") for e in brief.errors)
    assert brief.report_markdown


# --- 5) 최신성 ------------------------------------------------------------
def test_scenario5_stale_documents_are_flagged(tmp_settings):
    old = make_source(
        "ecos:old:1",
        title="2020년 자료",
        url="https://example.org/old",
        raw_text="매출은 1,000억원 입니다.",
        published_at=(datetime.now(timezone.utc).date() - timedelta(days=2000)),
    )
    brief = _orchestrator(
        tmp_settings,
        {ProviderName.ECOS: FakeConnector(ProviderName.ECOS, sources=[old])},
    ).run(REQUEST)

    kinds = {u.kind.value for u in brief.uncertainties}
    assert "staleness" in kinds
    assert "최신성" in brief.report_markdown
    assert "최신" in brief.report_markdown


def test_scenario5_missing_publish_date_is_reported(tmp_settings):
    undated = make_source(
        "ecos:undated:1",
        title="발행일 미상 자료",
        url="https://example.org/undated",
        raw_text="실적은 개선되었습니다.",
        published_at=None,
    )
    brief = _orchestrator(
        tmp_settings,
        {ProviderName.ECOS: FakeConnector(ProviderName.ECOS, sources=[undated])},
    ).run(REQUEST)
    kinds = {u.kind.value for u in brief.uncertainties}
    assert "missing_date" in kinds


# --- 6) 보안 --------------------------------------------------------------
def test_scenario6_no_api_key_in_api_responses_or_frontend(tmp_settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app())
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.text
    settings = get_settings()
    for secret in settings.secrets:
        assert secret not in body
    assert "NVIDIA_API_KEY" not in body

    static_dir = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
    for path in static_dir.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in ("NVIDIA_API_KEY", "DART_API_KEY", "ECOS_API_KEY", "KOSIS_API_KEY"):
            assert marker not in text, f"{path.name} 에 키 이름이 들어 있습니다."


def test_scenario6_logs_redact_secrets(tmp_settings):
    import logging

    from app.observability import SecretRedactingFilter

    # 실제 키가 아니라 마스킹 동작을 확인하기 위한 더미 값이다.
    secret = "DUMMY-REDACTION-KEY-000000000"
    flt = SecretRedactingFilter([secret])
    record = logging.LogRecord(
        "t", logging.INFO, "f", 1, f"키는 {secret} 입니다", None, None
    )
    assert flt.filter(record)
    assert secret not in record.getMessage()
    assert "***REDACTED***" in record.getMessage()


def test_scenario6_no_real_key_material_in_repository():
    """저장소 파일에 실제 키 형태의 문자열이 없어야 한다(예시/placeholder 제외)."""
    import re

    root = Path(__file__).resolve().parent.parent
    suspicious = re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|crtfc_key|apikey)\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{16,})"
    )
    placeholders = (
        "invalid_probe",
        "fake",
        "dummy",
        "example",
        "placeholder",
        "yourkey",
        "your_key",
        "changeme",
        "testkey",
        "probe",
        "xxxx",
        "<",
    )
    skip_dirs = {
        ".git", "data", "__pycache__", ".pytest_cache", "tests",
        "node_modules", ".wrangler", ".venv", "venv",
    }
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix.lower() in {".sqlite3", ".db", ".png", ".jpg", ".pdf"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = suspicious.search(line)
            if not match:
                continue
            value = match.group(2)
            if any(p in value.lower() for p in placeholders):
                continue
            raise AssertionError(
                f"{path.relative_to(root)} 에 키로 보이는 값이 있습니다: {match.group(0)[:70]}"
            )


def test_scenario6_tracked_files_contain_no_secret():
    """Git에 올라갈 파일 전체를 스캔해 키로 보이는 값이 없는지 확인한다."""
    import re
    import subprocess

    root = Path(__file__).resolve().parent.parent
    try:
        listing = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:  # noqa: BLE001 - git 이 없으면 검사만 건너뛴다
        pytest.skip("git 실행 가능하지 않아 검사를 건너뜁니다.")
    if listing.returncode != 0 or not listing.stdout.strip():
        pytest.skip("git 저장소가 아니어서 검사를 건너뜁니다.")

    suspicious = re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|crtfc_key|apikey|bearer)\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{16,})"
    )
    allowed = (
        "invalid_probe",
        "fake",
        "dummy",
        "example",
        "placeholder",
        "changeme",
        "probe",
        "xxxx",
        "not-used",
    )
    for rel in sorted(set(listing.stdout.split())):
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            match = suspicious.search(line)
            if not match:
                continue
            value = match.group(2).lower()
            if any(a in value for a in allowed):
                continue
            raise AssertionError(f"{rel} 에 키로 보이는 값이 있습니다: {match.group(0)[:70]}")


# --- 7) 샘플 모드 ---------------------------------------------------------
def test_scenario7_sample_data_is_distinguishable(tmp_settings):
    sample = make_source(
        "sample:demo:1",
        provider=ProviderName.SAMPLE,
        title="[샘플] 예시 자료",
        url="https://example.org/sample",
        provenance=Provenance.SAMPLE,
        raw_text="예시 지표는 12.5 % 입니다.",
    )
    live_text = "한국은행 기준금리는 2025-12 기준 3.25 % 입니다."
    live = make_source(
        "ecos:base_rate",
        title="[ECOS] 한국은행 기준금리 (2025-12)",
        raw_text=live_text,
        metrics=[
            _metric("한국은행 기준금리", 3.25, "%", "2025-12", "ecos:base_rate", live_text)
        ],
    )
    brief = _orchestrator(
        tmp_settings,
        {
            ProviderName.ECOS: FakeConnector(ProviderName.ECOS, sources=[live, sample]),
        },
    ).run(REQUEST)

    assert brief.live_source_count == 1
    assert brief.sample_source_count == 1
    md = brief.report_markdown
    assert "샘플" in md
    assert "실제 API/공식 자료" in md
    # 샘플 자료가 섞였다는 전역 경고가 있어야 한다.
    assert any(u.kind.value == "coverage_gap" for u in brief.uncertainties)
    # 샘플 자료에만 의존한 주장에는 개별 경고가 붙는다.
    sample_claims = [
        c for c in brief.claims if "sample:demo:1" in c.supporting_source_ids
    ]
    assert sample_claims
    assert any(u.kind.value == "ambiguity" for c in sample_claims for u in c.uncertainties)
