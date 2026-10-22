"""Evidence Checker - 주장별 지지 근거 / 반대 근거 / 상충 / 불확실성을 분리한다.

규칙(모두 결정론적이며 LLM 출력에 의존하지 않는다)
  1. 인용문은 반드시 원문 정규화 부분 문자열로 검증한다. 실패 시 근거에서 제거한다.
  2. 같은 지표인데 단위가 다르면 직접 비교하지 않고 경고한다.
  3. 같은 지표인데 기준기간이 다르면 직접 비교하지 않고 경고한다.
  4. 같은 지표/단위/기간인데 값이 다르면 양쪽을 모두 제시하고 원인을 단정하지 않는다.
  5. 발행일이 기준 시점보다 오래되면 최신성 경고를 남긴다.
  6. 발행일이 없으면 "발행일 불명"으로 표시한다.
  7. 샘플/데모 출처는 모든 주장에 "실제 데이터 아님" 경고를 함께 붙인다.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from ..models import (
    Claim,
    ClaimStatus,
    Severity,
    SourceDoc,
    Uncertainty,
    UncertaintyKind,
)
from ..observability import get_logger
from .parser import labels_match, verify_quote

log = get_logger(__name__)


def check(
    claims: Sequence[Claim],
    sources: Sequence[SourceDoc],
    *,
    stale_after_days: int = 400,
    today: Optional[date] = None,
) -> Tuple[List[Claim], List[Uncertainty]]:
    """주장을 검증하고 전역 불확실성 목록을 만든다."""
    ref = today or date.today()
    by_id: Dict[str, SourceDoc] = {s.source_id: s for s in sources}
    global_uncertainties: List[Uncertainty] = []

    checked: List[Claim] = []
    for claim in claims:
        claim = claim.model_copy(deep=True)
        claim.uncertainties = []

        # 1) 인용문 원문 대조
        verified_quotes = []
        for quote in claim.quotes:
            source = by_id.get(quote.source_id)
            if source is None:
                claim.uncertainties.append(
                    Uncertainty(
                        kind=UncertaintyKind.MISSING_SOURCE,
                        severity=Severity.CRITICAL,
                        message=f"인용문의 출처({quote.source_id})를 찾을 수 없습니다.",
                        source_ids=[quote.source_id],
                    )
                )
                continue
            if verify_quote(quote.text, source.raw_text):
                quote.verified = True
                verified_quotes.append(quote)
            else:
                claim.uncertainties.append(
                    Uncertainty(
                        kind=UncertaintyKind.UNVERIFIED_QUOTE,
                        severity=Severity.CRITICAL,
                        message=(
                            f"인용문이 원문에서 확인되지 않아 근거에서 제외했습니다: {quote.text[:60]}"
                        ),
                        source_ids=[quote.source_id],
                    )
                )
        claim.quotes = verified_quotes

        # 2) 근거가 모두 사라진 주장은 미검증으로 강등
        kept_support = [
            sid
            for sid in claim.supporting_source_ids
            if any(q.source_id == sid for q in verified_quotes) or _has_metric_in_source(claim, sid, by_id)
        ]
        if claim.origin == "llm":
            claim.supporting_source_ids = kept_support
            if not kept_support:
                claim.status = ClaimStatus.UNVERIFIED

        if not claim.supporting_source_ids and not claim.contradicting_source_ids:
            claim.status = ClaimStatus.UNVERIFIED
            claim.uncertainties.append(
                Uncertainty(
                    kind=UncertaintyKind.MISSING_SOURCE,
                    severity=Severity.CRITICAL,
                    message="확인 가능한 출처가 없어 '근거 부족'으로 표시합니다.",
                    source_ids=[],
                )
            )

        # 3) 샘플/데모 출처 경고
        for sid in list(claim.supporting_source_ids) + list(claim.contradicting_source_ids):
            source = by_id.get(sid)
            if source is None:
                continue
            if not source.provenance.is_real:
                claim.uncertainties.append(
                    Uncertainty(
                        kind=UncertaintyKind.AMBIGUITY,
                        severity=Severity.WARNING,
                        message=(
                            f"이 주장은 '{source.provenance.label}' 인 자료에 근거합니다. "
                            "실제 API 결과가 아닙니다."
                        ),
                        source_ids=[sid],
                    )
                )

        # 4) 단위/기간 불일치 경고 (비교하지 않은 출처)
        for sid in claim.incomparable_source_ids:
            source = by_id.get(sid)
            if source is None:
                continue
            reason = claim.incomparable_reasons.get(sid, "period")
            if reason == "unit":
                kind = UncertaintyKind.UNIT_MISMATCH
                message = (
                    f"'{source.source_id}' 은 단위가 달라 직접 비교하지 않았습니다. "
                    "단위를 환산하지 않고 원값만 제시합니다."
                )
            else:
                kind = UncertaintyKind.PERIOD_MISMATCH
                message = (
                    f"'{source.source_id}' 은 기준기간이 달라 직접 비교하지 않았습니다."
                )
            claim.uncertainties.append(
                Uncertainty(
                    kind=kind,
                    severity=Severity.WARNING,
                    message=message,
                    source_ids=[sid],
                    metric_label=claim.metrics[0].label if claim.metrics else None,
                )
            )

        checked.append(claim)

    # 5) 출처 단위 최신성/발행일 검사
    for source in sources:
        if source.error:
            global_uncertainties.append(
                Uncertainty(
                    kind=UncertaintyKind.API_ERROR,
                    severity=Severity.CRITICAL,
                    message=f"{source.title} 수집에 실패했습니다: {source.error}",
                    source_ids=[source.source_id],
                )
            )
            continue
        if source.published_at is None:
            global_uncertainties.append(
                Uncertainty(
                    kind=UncertaintyKind.MISSING_DATE,
                    severity=Severity.WARNING,
                    message=f"'{source.title}' 의 발행일을 확인할 수 없습니다. 최신성 판단이 어렵습니다.",
                    source_ids=[source.source_id],
                )
            )
        else:
            age = (ref - source.published_at).days
            if age > stale_after_days:
                global_uncertainties.append(
                    Uncertainty(
                        kind=UncertaintyKind.STALENESS,
                        severity=Severity.WARNING,
                        message=(
                            f"'{source.title}' 는 {source.published_at.isoformat()} 자료로 "
                            f"{age}일 전 것입니다. 최신 정보가 아닐 수 있습니다."
                        ),
                        source_ids=[source.source_id],
                    )
                )
            elif age < 0:
                global_uncertainties.append(
                    Uncertainty(
                        kind=UncertaintyKind.AMBIGUITY,
                        severity=Severity.INFO,
                        message=f"'{source.title}' 의 발행일이 미래 날짜로 표시되어 있습니다.",
                        source_ids=[source.source_id],
                    )
                )

    # 6) 상충 자료: 같은 지표/단위/기간인데 값이 다른 경우
    global_uncertainties.extend(_detect_conflicts(checked, by_id))

    # 7) 표본(샘플) 자료가 섞여 있으면 전체 경고
    sample_ids = [s.source_id for s in sources if not s.provenance.is_real]
    if sample_ids:
        global_uncertainties.append(
            Uncertainty(
                kind=UncertaintyKind.COVERAGE_GAP,
                severity=Severity.WARNING,
                message=(
                    "샘플/데모 자료가 포함되어 있습니다. 실제 API 결과와 구분해 읽으십시오."
                ),
                source_ids=sample_ids,
            )
        )

    return checked, global_uncertainties


def _has_metric_in_source(claim: Claim, source_id: str, by_id: Dict[str, SourceDoc]) -> bool:
    source = by_id.get(source_id)
    if source is None:
        return False
    for metric in claim.metrics:
        if metric.source_id != source_id:
            continue
        if metric.raw_value and metric.raw_value in source.raw_text:
            return True
    return False


def _detect_conflicts(
    claims: Sequence[Claim], by_id: Dict[str, SourceDoc]
) -> List[Uncertainty]:
    out: List[Uncertainty] = []
    seen: set = set()
    for claim in claims:
        for metric in claim.metrics:
            if not metric.source_id:
                continue
            for other in claim.metrics:
                if other is metric or not other.source_id:
                    continue
                if not labels_match(metric.label, other.label):
                    continue
                if (metric.unit or "") != (other.unit or ""):
                    continue
                if (metric.period or "") != (other.period or ""):
                    continue
                if abs(metric.value - other.value) < 1e-9:
                    continue
                key = tuple(sorted([metric.source_id, other.source_id, metric.label]))
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    Uncertainty(
                        kind=UncertaintyKind.CONFLICT,
                        severity=Severity.WARNING,
                        message=(
                            f"'{metric.label}' ({metric.period or '기간 미상'}) 값이 "
                            f"{metric.raw_value}{(' ' + metric.unit) if metric.unit else ''} 와 "
                            f"{other.raw_value}{(' ' + other.unit) if other.unit else ''} 로 "
                            "상이합니다. 양쪽을 모두 제시하며, 원인 단정은 하지 않습니다."
                        ),
                        source_ids=[metric.source_id, other.source_id],
                        metric_label=metric.label,
                    )
                )
    return out
