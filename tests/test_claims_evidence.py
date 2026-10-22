"""주장 추출 · 근거 검증 단위 테스트."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import (  # noqa: E402
    ClaimStatus,
    Metric,
    Provenance,
    Severity,
    UncertaintyKind,
)
from app.pipeline import claims as claim_mod  # noqa: E402
from app.pipeline import evidence as evidence_mod  # noqa: E402
from tests.helpers import make_source  # noqa: E402


def _metric(label, value, unit, period, source_id):
    return Metric(
        label=label,
        value=value,
        raw_value=str(value),
        unit=unit,
        period=period,
        source_id=source_id,
        context=f"{label} {period} {value}{unit}",
    )


def test_group_observations_merges_similar_labels():
    a = claim_mod.Observation("한국은행 기준금리", 3.25, "3.25", "%", "2025-12", "s1")
    b = claim_mod.Observation("한국은행 기준금리", 3.25, "3.25", "%", "2025-12", "s2")
    c = claim_mod.Observation("원/달러 환율", 1300.0, "1300", "원", "2025-12", "s3")
    groups = claim_mod.group_observations([a, b, c])
    assert len(groups) == 2
    assert len(groups[0].observations) == 2


def test_build_claims_marks_support_and_contradiction():
    s1 = make_source(
        "s1",
        raw_text="한국은행 기준금리는 2025-12 기준 3.25 % 입니다.",
        metrics=[_metric("한국은행 기준금리", 3.25, "%", "2025-12", "s1")],
    )
    s2 = make_source(
        "s2",
        raw_text="한국은행 기준금리는 2025-12 기준 3.25 % 입니다.",
        metrics=[_metric("한국은행 기준금리", 3.25, "%", "2025-12", "s2")],
    )
    s3 = make_source(
        "s3",
        raw_text="한국은행 기준금리는 2025-12 기준 3.50 % 입니다.",
        metrics=[_metric("한국은행 기준금리", 3.50, "%", "2025-12", "s3")],
    )
    built = claim_mod.build_claims([s1, s2, s3])
    assert built
    claim = built[0]
    assert claim.status is ClaimStatus.CONTESTED
    assert "s1" in claim.supporting_source_ids or "s1" in claim.contradicting_source_ids
    assert len(claim.supporting_source_ids) >= 1
    assert len(claim.contradicting_source_ids) >= 1


def test_build_claims_does_not_compare_different_units_or_periods():
    s1 = make_source(
        "s1",
        raw_text="영업이익 1000억원",
        metrics=[_metric("영업이익", 1000.0, "억원", "2025", "s1")],
    )
    s2 = make_source(
        "s2",
        raw_text="영업이익 1000백만원",
        metrics=[_metric("영업이익", 1000.0, "백만원", "2025", "s2")],
    )
    s3 = make_source(
        "s3",
        raw_text="영업이익 1200억원",
        metrics=[_metric("영업이익", 1200.0, "억원", "2024", "s3")],
    )
    built = claim_mod.build_claims([s1, s2, s3])
    claim = built[0]
    assert claim.status is ClaimStatus.SUPPORTED
    assert not claim.contradicting_source_ids
    assert set(claim.incomparable_source_ids) == {"s2", "s3"}
    assert claim.incomparable_reasons["s2"] == "unit"
    assert claim.incomparable_reasons["s3"] == "period"
    # 비교하지 않은 사유가 경고로도 남아야 한다.
    checked, _ = evidence_mod.check(built, [s1, s2, s3])
    kinds = {u.kind for u in checked[0].uncertainties}
    assert UncertaintyKind.UNIT_MISMATCH in kinds
    assert UncertaintyKind.PERIOD_MISMATCH in kinds


def test_evidence_rejects_quote_not_in_source():
    source = make_source("s1", raw_text="한국은행 기준금리는 3.25 % 입니다.")
    claim = claim_mod.Claim(
        claim_id="c1",
        text="주장",
        supporting_source_ids=["s1"],
        quotes=[
            __import__("app.models", fromlist=["Quote"]).Quote(
                text="존재하지 않는 인용문 9.99 %", source_id="s1"
            )
        ],
    )
    checked, uncertainties = evidence_mod.check([claim], [source])
    assert checked[0].quotes == []
    kinds = {u.kind for u in checked[0].uncertainties}
    assert UncertaintyKind.UNVERIFIED_QUOTE in kinds
    assert checked[0].status is ClaimStatus.UNVERIFIED


def test_evidence_flags_stale_source():
    old = make_source(
        "s1",
        raw_text="매출 1000억원",
        published_at=datetime.now(timezone.utc).date() - timedelta(days=1000),
    )
    checked, uncertainties = evidence_mod.check([], [old], stale_after_days=400)
    assert any(u.kind is UncertaintyKind.STALENESS for u in uncertainties)


def test_evidence_flags_conflicting_sources():
    s1 = make_source(
        "s1",
        raw_text="매출 1000억원",
        metrics=[_metric("매출", 1000.0, "억원", "2025", "s1")],
    )
    s2 = make_source(
        "s2",
        raw_text="매출 1200억원",
        metrics=[_metric("매출", 1200.0, "억원", "2025", "s2")],
    )
    claims = claim_mod.build_claims([s1, s2])
    checked, uncertainties = evidence_mod.check(claims, [s1, s2])
    assert any(u.kind is UncertaintyKind.CONFLICT for u in uncertainties)


def test_evidence_marks_sample_source_warning():
    sample = make_source(
        "sample:1",
        provenance=Provenance.SAMPLE,
        raw_text="예시 지표 12.5 %",
        metrics=[_metric("예시 지표", 12.5, "%", "2025", "sample:1")],
    )
    claims = claim_mod.build_claims([sample])
    checked, uncertainties = evidence_mod.check(claims, [sample])
    assert any(u.kind is UncertaintyKind.AMBIGUITY for u in checked[0].uncertainties)
    assert any(u.kind is UncertaintyKind.COVERAGE_GAP for u in uncertainties)


def test_llm_claims_with_fake_quote_are_dropped():
    source = make_source("s1", raw_text="한국은행 기준금리는 3.25 % 입니다.")
    result = claim_mod.merge_llm_claims(
        [
            {
                "text": "허위로 만들어진 주장",
                "source_id": "s1",
                "quote": "존재하지 않는 문장",
                "metrics": [{"label": "한국은행 기준금리", "value": 99.9, "raw_value": "99.9"}],
            }
        ],
        [source],
    )
    assert result == []


def test_llm_claims_with_valid_quote_are_kept():
    text = "한국은행 기준금리는 3.25 % 입니다."
    source = make_source("s1", raw_text=text)
    result = claim_mod.merge_llm_claims(
        [
            {
                "text": "기준금리는 3.25%",
                "source_id": "s1",
                "quote": text,
                "metrics": [
                    {
                        "label": "한국은행 기준금리",
                        "value": 3.25,
                        "raw_value": "3.25",
                        "unit": "%",
                        "period": "2025-12",
                    }
                ],
            }
        ],
        [source],
    )
    assert len(result) == 1
    assert result[0].origin == "llm"
    assert result[0].quotes[0].verified
