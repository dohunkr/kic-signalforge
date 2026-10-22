"""전망 분석 - 주장별 방향성 판정과 상승/하락 확률 산출.

원칙
  * 확률은 LLM 추측이 아니라 **근거의 방향 가중치**로 계산한다.
  * LLM 은 각 주장의 방향만 분류하고, 반드시 이미 원문 대조가 끝난 인용문만 근거로 낸다.
  * LLM 이 실패하면 결정론적 폴백(수치의 전 기간 대비 추세)으로 판정한다.
  * 확률과 기대수익률에는 항상 근거 수치·방법론·한계를 함께 붙인다.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..models import (
    Claim,
    ClaimOutlook,
    ClaimStatus,
    OutlookDirection,
    OutlookSummary,
    SourceDoc,
    Uncertainty,
)
from ..observability import get_logger

log = get_logger(__name__)

#: 확률 계산 가중치
STRENGTH_WEIGHT = {1: 1.0, 2: 1.8, 3: 2.6}

#: 방법론 설명(사용자 화면에 그대로 노출)
METHOD_TEXT = (
    "AI가 원문 대조가 끝난 주장마다 상승/하락/중립 방향과 근거 강도(1~3)를 붙이고, "
    "강도×근거 개수로 가중치를 합산합니다. "
    "확률은 가중치 비율에 '근거 커버리지(근거가 있는 주장 비율)'를 곱해 50% 쪽으로 수축한 값이며, "
    "기대수익률은 과거 3개월 수익률 분포의 상위/하위 25%를 결합해 계산합니다."
)


def _claim_payload(claim: Claim) -> Dict[str, Any]:
    quotes = [q.text for q in claim.quotes if q.verified]
    metrics = [
        {
            "label": m.label,
            "value": m.value,
            "unit": m.unit,
            "period": m.period,
        }
        for m in claim.metrics[:3]
    ]
    return {
        "claim_id": claim.claim_id,
        "text": claim.text,
        "status": claim.status.value,
        "quotes": quotes[:2],
        "metrics": metrics,
        "source_ids": claim.supporting_source_ids[:3],
    }


def classify_with_llm(
    claims: Sequence[Claim],
    *,
    llm: Optional[Any] = None,
    max_tokens: int = 700,
) -> Tuple[List[ClaimOutlook], List[str]]:
    """LLM 으로 주장별 방향을 판정한다. 실패 시 빈 목록을 돌려준다."""
    notes: List[str] = []
    if llm is None or not getattr(llm, "available", False):
        return [], notes

    usable = [c for c in claims if c.quotes and any(q.verified for q in c.quotes)]
    if not usable:
        return [], notes

    system = (
        "당신은 금융 리서치 보조 에이전트입니다. 주어진 주장마다 방향만 분류하십시오. "
        "방향은 상승(up) / 하락(down) / 중립(neutral) 중 하나입니다. "
        "근거 강도(strength)는 1(약)~3(강)입니다. "
        "주장은 이미 원문 대조가 끝난 항목이므로 인용문을 다시 옮기지 말고, "
        "claim_id 와 방향/강도/한 줄 근거만 출력하십시오. "
        "답변은 JSON 배열만 출력하십시오."
    )
    payload = json.dumps([_claim_payload(c) for c in usable], ensure_ascii=False)
    user = (
        "아래 주장들을 분류하십시오.\n"
        "출력 형식: "
        '[{"claim_id":"c001","direction":"up","strength":2,'
        '"rationale":"한 줄 근거"}]\n\n'
        f"{payload}"
    )

    try:
        raw = llm.chat_json(system, user, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"LLM 방향 판정 실패: {type(exc).__name__}")
        return [], notes

    items = raw.get("outlooks") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        items = raw.get("claims") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        notes.append("LLM 이 방향 판정 결과를 JSON 배열로 반환하지 않았습니다.")
        return [], notes

    by_id = {c.claim_id: c for c in usable}
    out: List[ClaimOutlook] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        claim_id = str(item.get("claim_id") or "").strip()
        claim = by_id.get(claim_id)
        if claim is None:
            continue
        direction = _parse_direction(item.get("direction"))
        if direction is None:
            continue
        # 주장 자체가 이미 원문 대조를 통과한 항목이므로, 인용문 재복사는 요구하지 않는다.
        try:
            strength = int(item.get("strength") or 1)
        except (TypeError, ValueError):
            strength = 1
        strength = max(1, min(3, strength))
        out.append(
            ClaimOutlook(
                claim_id=claim_id,
                direction=direction,
                strength=strength,
                rationale=str(item.get("rationale") or "")[:160],
                source_ids=claim.supporting_source_ids[:3],
                method="llm",
            )
        )
    if not out:
        notes.append("LLM 이 검증 가능한 방향 판정을 반환하지 않았습니다.")
    return out, notes


def _parse_direction(value: Any) -> Optional[OutlookDirection]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in ("up", "bull", "bullish", "상승", "상방"):
        return OutlookDirection.UP
    if text in ("down", "bear", "bearish", "하락", "하방"):
        return OutlookDirection.DOWN
    if text in ("neutral", "mixed", "중립", "보합"):
        return OutlookDirection.NEUTRAL
    return None


def classify_deterministically(claims: Sequence[Claim]) -> List[ClaimOutlook]:
    """LLM 없이 방향을 추정한다(수치의 전 기간 대비 추세 기반, 강도는 낮게 둔다)."""
    out: List[ClaimOutlook] = []
    for claim in claims:
        metrics = [m for m in claim.metrics if m.period]
        if len(metrics) < 2:
            out.append(
                ClaimOutlook(
                    claim_id=claim.claim_id,
                    direction=OutlookDirection.NEUTRAL,
                    strength=1,
                    rationale="추세 비교 가능한 수치가 없어 중립으로 분류",
                    source_ids=claim.supporting_source_ids[:3],
                    method="deterministic",
                )
            )
            continue
        ordered = sorted(metrics, key=lambda m: m.period or "")
        latest, previous = ordered[-1], ordered[-2]
        if latest.value == previous.value:
            direction = OutlookDirection.NEUTRAL
            rationale = "직전 기간과 동일"
        elif latest.value > previous.value:
            direction = OutlookDirection.UP
            rationale = f"{previous.period} {previous.raw_value} → {latest.period} {latest.raw_value}"
        else:
            direction = OutlookDirection.DOWN
            rationale = f"{previous.period} {previous.raw_value} → {latest.period} {latest.raw_value}"
        out.append(
            ClaimOutlook(
                claim_id=claim.claim_id,
                direction=direction,
                strength=2,
                rationale=rationale[:160],
                source_ids=claim.supporting_source_ids[:3],
                method="deterministic",
            )
        )
    return out


def summarize(
    outlooks: Sequence[ClaimOutlook],
    claims: Sequence[Claim],
    uncertainties: Sequence[Uncertainty],
    *,
    expected_up: float = 0.0,
    expected_down: float = 0.0,
) -> OutlookSummary:
    """가중치로 상승/하락 확률과 기대수익률을 계산한다."""
    by_id = {c.claim_id: c for c in claims}
    bull = bear = neutral = 0.0
    bull_lines: List[str] = []
    bear_lines: List[str] = []
    neutral_lines: List[str] = []

    for outlook in outlooks:
        claim = by_id.get(outlook.claim_id)
        if claim is None:
            continue
        weight = STRENGTH_WEIGHT.get(outlook.strength, 1.0)
        # 근거가 없는主张은 확률에 반영하지 않는다.
        if not claim.supporting_source_ids and not claim.contradicting_source_ids:
            continue
        line = f"{claim.text[:80]}"
        if outlook.rationale:
            line += f" — {outlook.rationale[:60]}"
        if outlook.direction is OutlookDirection.UP:
            bull += weight
            bull_lines.append(line)
        elif outlook.direction is OutlookDirection.DOWN:
            bear += weight
            bear_lines.append(line)
        else:
            neutral += weight
            neutral_lines.append(line)

    total = bull + bear + neutral
    if total <= 0:
        raw_up = 0.5
    else:
        # 중립은 50/50 으로 나눠 반영한다.
        raw_up = (bull + neutral * 0.5) / total

    # 근거 커버리지: 검증된 근거가 있는 주장 비율
    judged = {o.claim_id for o in outlooks}
    scored = [c for c in claims if c.claim_id in judged]
    covered = [
        c for c in scored if c.supporting_source_ids or c.contradicting_source_ids
    ]
    coverage = (len(covered) / len(scored)) if scored else 0.0

    # 불확실성 패널티: 경고가 많을수록 확률을 50% 쪽으로 수축한다.
    penalty = min(0.5, 0.06 * len(uncertainties))
    confidence = max(0.0, min(1.0, coverage * (1.0 - penalty)))

    prob_up = 0.5 + (raw_up - 0.5) * confidence
    prob_up = round(max(0.05, min(0.95, prob_up)), 4)
    prob_down = round(1.0 - prob_up, 4)

    expected_return = prob_up * expected_up - prob_down * expected_down

    notes = [
        f"근거 커버리지 {coverage:.0%} (판정 대상 {len(scored)}건 중 {len(covered)}건)",
        f"불확실성 {len(uncertainties)}건으로 확률을 50% 쪽으로 수축했습니다.",
    ]
    if not outlooks:
        notes.append("방향 판정 근거가 없어 50/50으로 두었습니다.")

    return OutlookSummary(
        probability_up=prob_up,
        probability_down=prob_down,
        expected_return=round(expected_return, 2),
        confidence=round(confidence, 4),
        method=METHOD_TEXT,
        bull_evidence=bull_lines[:8],
        bear_evidence=bear_lines[:8],
        neutral_evidence=neutral_lines[:5],
        notes=notes,
    )
