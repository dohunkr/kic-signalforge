"""Report Writer - 출처 링크가 포함된 브리핑을 생성한다.

원칙
  * 근거가 없는 주장은 단정하지 않고 "확인 불가/근거 부족"으로 표시한다.
  * 모든 수치/인용에는 출처 ID와 링크를 붙인다.
  * 샘플/데모 자료는 본문에서 계속 구분해 표시한다.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from ..models import (
    Briefing,
    Claim,
    ClaimStatus,
    ConnectorError,
    Severity,
    SourceDoc,
    Uncertainty,
    UncertaintyKind,
)

_STATUS_KO = {
    "completed": "완료",
    "partial": "부분 완료(일부 자료 수집 실패)",
    "insufficient_evidence": "근거 부족(확인 불가)",
    "failed": "실패",
}

_CLAIM_STATUS_KO = {
    ClaimStatus.SUPPORTED: "근거 있음",
    ClaimStatus.CONTESTED: "상충 근거 있음",
    ClaimStatus.UNVERIFIED: "근거 부족(확인 불가)",
}

_KIND_KO = {
    UncertaintyKind.STALENESS: "최신성",
    UncertaintyKind.MISSING_DATE: "발행일 불명",
    UncertaintyKind.CONFLICT: "자료 상충",
    UncertaintyKind.UNIT_MISMATCH: "단위 불일치",
    UncertaintyKind.PERIOD_MISMATCH: "기준기간 불일치",
    UncertaintyKind.MISSING_SOURCE: "출처 없음",
    UncertaintyKind.API_ERROR: "API 실패",
    UncertaintyKind.COVERAGE_GAP: "자료 공백",
    UncertaintyKind.UNVERIFIED_QUOTE: "인용 미검증",
    UncertaintyKind.AMBIGUITY: "주의",
    UncertaintyKind.LLM_UNAVAILABLE: "LLM 미사용",
}

_SEVERITY_MARK = {
    Severity.INFO: "ℹ️",
    Severity.WARNING: "⚠️",
    Severity.CRITICAL: "⛔",
}


def render(briefing: Briefing) -> str:
    """브리핑 마크다운을 생성한다."""
    lines: List[str] = []
    source_index = _source_index(briefing.sources)

    lines.append("# KIC SignalForge 리서치 브리핑")
    lines.append("")
    lines.append(f"- **질문**: {briefing.question}")
    if briefing.topic:
        lines.append(f"- **주제**: {briefing.topic}")
    if briefing.period:
        lines.append(f"- **요청 기간**: {briefing.period}")
    lines.append(f"- **실행 시각**: {briefing.stats.started_at.astimezone().isoformat(timespec='seconds')}")
    lines.append(f"- **소요 시간**: {briefing.stats.duration_seconds:.1f}초")
    lines.append(f"- **상태**: **{_STATUS_KO.get(briefing.status.value, briefing.status.value)}**")
    lines.append(
        f"- **자료 구성**: 전체 {len(briefing.sources)}건 "
        f"(실제 API/공식 자료 {briefing.live_source_count}건, "
        f"샘플·데모 {briefing.sample_source_count}건)"
    )
    llm_note = "사용함" if briefing.llm.used else "사용 안 함(결정론적 분석만 수행)"
    llm_model = f" / 모델: {briefing.llm.model}" if briefing.llm.model else ""
    lines.append(f"- **LLM(NVIDIA NIM)**: {llm_note}{llm_model}")
    if briefing.llm.reason:
        lines.append(f"  - 사유: {briefing.llm.reason}")
    lines.append("")

    # 1) 요약
    lines.append("## 1. 요약")
    lines.append("")
    lines.extend(_summary_lines(briefing))
    lines.append("")

    # 2) 출처 목록
    lines.append("## 2. 자료 출처 목록")
    lines.append("")
    if not briefing.sources:
        lines.append("수집된 출처가 없습니다.")
    else:
        lines.append("| 출처ID | 자료 | 제공기관 | 발행일 | 기준기간 | 단위 | 구분 | 링크 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for source in briefing.sources:
            lines.append(
                "| `{sid}` | {title} | {provider} | {published} | {period} | {unit} | {prov} | [원문]({url}) |".format(
                    sid=source.source_id,
                    title=_escape(source.title),
                    provider=source.provider.value,
                    published=source.published_at.isoformat() if source.published_at else "미확인",
                    period=source.period_label or "미확인",
                    unit=source.unit or "미확인",
                    prov=source.provenance.label,
                    url=source.url,
                )
            )
    lines.append("")

    # 3) 주장별 근거/반대 근거
    lines.append("## 3. 핵심 주장별 근거 · 반대 근거")
    lines.append("")
    if not briefing.claims:
        lines.append(
            "확인 가능한 근거가 없어 주장을 생성하지 않았습니다. "
            "**확인 불가(근거 부족)** 상태입니다."
        )
    else:
        for claim in briefing.claims:
            lines.extend(_claim_block(claim, source_index))
    lines.append("")

    # 4) 불확실성
    lines.append("## 4. 불확실성 · 자료 상충 · 최신성 경고")
    lines.append("")
    if not briefing.uncertainties and not any(c.uncertainties for c in briefing.claims):
        lines.append("등록된 경고가 없습니다. (없다고 해서 근거가 충분하다는 뜻은 아닙니다.)")
    else:
        for unc in briefing.uncertainties:
            lines.append(_uncertainty_line(unc))
        for claim in briefing.claims:
            for unc in claim.uncertainties:
                lines.append(_uncertainty_line(unc, claim_id=claim.claim_id))
    lines.append("")

    # 5) 실패/공백
    lines.append("## 5. 데이터 공백 및 실패한 API")
    lines.append("")
    if briefing.errors:
        lines.append("| 제공기관 | 오류코드 | 메시지 |")
        lines.append("|---|---|---|")
        for err in briefing.errors:
            lines.append(
                f"| {err.provider.value} | `{err.code}` | {_escape(err.message)} |"
            )
    else:
        lines.append("실패한 API 호출이 없습니다.")
    lines.append("")
    if briefing.data_gaps:
        lines.append("**확인하지 못한 항목**")
        lines.append("")
        for gap in briefing.data_gaps:
            lines.append(f"- {gap}")
        lines.append("")
    else:
        lines.append("확인하지 못한 항목이 별도로 기록되지 않았습니다.")
        lines.append("")

    # 6) 다음 단계
    lines.append("## 6. 사용자가 검토할 사항")
    lines.append("")
    lines.append("- 각 주장의 지지/반대 근거 출처를 직접 열어 원문 대조를 하십시오.")
    lines.append("- 단위·기준기간이 다른 수치는 이 브리핑에서 직접 비교하지 않았습니다.")
    lines.append("- 최신성이 의심되는 자료는 발행일을 확인하고 최신 공시로 교체하십시오.")
    lines.append("")

    # 7) 면책
    lines.append("## 7. 유의사항")
    lines.append("")
    lines.append(briefing.disclaimer)
    lines.append("")
    lines.append(
        "본 문서는 NVIDIA NIM 모델이 아니라 **검증 규칙과 공개 자료**를 기반으로 생성되었으며, "
        "LLM은 문장 정리 단계에서만 사용됩니다."
    )
    lines.append("")
    return "\n".join(lines)


# --- 내부 헬퍼 -----------------------------------------------------------
def _summary_lines(briefing: Briefing) -> List[str]:
    supported = [c for c in briefing.claims if c.status is ClaimStatus.SUPPORTED]
    contested = [c for c in briefing.claims if c.status is ClaimStatus.CONTESTED]
    unverified = [c for c in briefing.claims if c.status is ClaimStatus.UNVERIFIED]

    if not briefing.sources:
        return [
            "**자료를 하나도 확보하지 못했습니다.** 따라서 어떠한 결론도 제시하지 않습니다.",
            "이유는 5장의 실패한 API 목록을 확인하십시오.",
        ]

    lines = []
    if supported:
        lines.append(f"- 근거가 확인된 주장: **{len(supported)}건**")
    if contested:
        lines.append(f"- 상충하는 근거가 있는 주장: **{len(contested)}건**")
    if unverified:
        lines.append(f"- 근거 부족(확인 불가) 주장: **{len(unverified)}건**")
    if briefing.sample_source_count:
        lines.append(
            f"- ⚠️ 샘플/데모 자료 {briefing.sample_source_count}건이 포함되어 있습니다. "
            "실제 API 결과와 구분해 읽으십시오."
        )
    if briefing.errors:
        lines.append(f"- ⚠️ 자료 수집 실패 {len(briefing.errors)}건이 있습니다.")
    lines.append("- 최종 투자 판단과 매매 여부는 사용자가 검토하도록 남겨두었습니다.")
    return lines


def _claim_block(claim: Claim, source_index: Dict[str, SourceDoc]) -> List[str]:
    lines = [f"### {claim.claim_id} · {_CLAIM_STATUS_KO.get(claim.status, claim.status.value)}"]
    lines.append("")
    lines.append(f"- **주장**: {claim.text}")
    if claim.metrics:
        parts = []
        for metric in claim.metrics:
            unit = f" {metric.unit}" if metric.unit else ""
            period = f" ({metric.period})" if metric.period else ""
            parts.append(
                f"{metric.label}: {metric.raw_value}{unit}{period} "
                f"`{metric.source_id}`"
            )
        lines.append("- **확인된 수치**:")
        for part in parts:
            lines.append(f"  - {part}")
    lines.append("")

    lines.append(f"- **지지 근거** ({len(claim.supporting_source_ids)}건)")
    if claim.supporting_source_ids:
        for sid in claim.supporting_source_ids:
            lines.append(f"  - {_source_bullet(sid, source_index)}")
    else:
        lines.append("  - 확인된 지지 근거 없음 (확인 불가)")
    lines.append("")

    lines.append(f"- **반대 근거** ({len(claim.contradicting_source_ids)}건)")
    if claim.contradicting_source_ids:
        for sid in claim.contradicting_source_ids:
            lines.append(f"  - {_source_bullet(sid, source_index)}")
    else:
        lines.append("  - 확인된 반대 근거 없음 (이것이 반대 근거가 없다는 뜻은 아닙니다)")
    lines.append("")

    if claim.incomparable_source_ids:
        lines.append("- **직접 비교하지 않은 출처** (단위/기간 불일치)")
        for sid in claim.incomparable_source_ids:
            lines.append(f"  - {_source_bullet(sid, source_index)}")
        lines.append("")

    verified = [q for q in claim.quotes if q.verified]
    if verified:
        lines.append("- **원문 인용**")
        for quote in verified[:3]:
            source = source_index.get(quote.source_id)
            title = source.title if source else quote.source_id
            lines.append(f"  - \"{_escape(quote.text[:220])}\" — [{title}]({source.url})" if source else f"  - \"{_escape(quote.text[:220])}\"")
        lines.append("")

    return lines


def _source_bullet(source_id: str, source_index: Dict[str, SourceDoc]) -> str:
    source = source_index.get(source_id)
    if source is None:
        return f"`{source_id}` (출처 정보 없음)"
    published = source.published_at.isoformat() if source.published_at else "발행일 미확인"
    period = source.period_label or "기준기간 미확인"
    unit = source.unit or "단위 미확인"
    return (
        f"[{_escape(source.title)}]({source.url}) — `{source_id}` / {published} / "
        f"기준기간 {period} / {unit} / {source.provenance.label}"
    )


def _uncertainty_line(unc: Uncertainty, claim_id: str = "") -> str:
    mark = _SEVERITY_MARK.get(unc.severity, "•")
    kind = _KIND_KO.get(unc.kind, unc.kind.value)
    prefix = f"  - [{claim_id}] " if claim_id else "- "
    return f"{prefix}{mark} **{kind}**: {unc.message}"


def _source_index(sources: Sequence[SourceDoc]) -> Dict[str, SourceDoc]:
    return {s.source_id: s for s in sources}


def _escape(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def data_gaps_from(
    claims: Sequence[Claim],
    errors: Sequence[ConnectorError],
    sources: Sequence[SourceDoc],
) -> List[str]:
    """확인하지 못한 항목을 정리한다."""
    gaps: List[str] = []
    for claim in claims:
        if claim.status is ClaimStatus.UNVERIFIED:
            gaps.append(f"{claim.claim_id}: '{claim.text[:80]}' — 출처로 확인되지 않음")
    for err in errors:
        gaps.append(f"{err.provider.value}: {err.message}")
    if not sources:
        gaps.append("수집된 자료가 없음")
    return gaps
