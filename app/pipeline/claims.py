"""Claim Extractor - 핵심 주장과 수치를 구조화한다.

두 경로를 지원한다.
  1) 결정론적 경로(기본): 수집된 자료의 수치/문장에서 관측치를 뽑아 그룹핑한다.
  2) LLM 보조 경로(선택): NIM 이 후보 주장을 만들면 검증 단계에서 원문 대조를 거친다.

LLM 이 없을 때도 동작해야 하며, 없는 데이터에서 주장을 만들어내지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Tuple

from ..models import Claim, ClaimStatus, Metric, ProviderName, Quote, SourceDoc
from ..observability import get_logger
from .parser import (
    extract_metrics_from_text,
    labels_match,
    normalize_label,
    split_sentences,
    verify_quote,
)

log = get_logger(__name__)

#: 동일 지표로 묶을 때의 라벨 유사도 임계값
LABEL_MATCH_THRESHOLD = 0.6

#: 같은 지표/단위/기간에서 "같은 값"으로 볼 상대 허용오차 (기본 0.5%)
DEFAULT_TOLERANCE = 0.005


@dataclass
class Observation:
    """출처 하나에서 추출한 개별 수치 관측치."""

    label: str
    value: float
    raw_value: str
    unit: Optional[str]
    period: Optional[str]
    source_id: str
    context: str = ""
    quote: str = ""

    @property
    def key(self) -> str:
        return normalize_label(self.label)


@dataclass
class ClaimGroup:
    """같은 지표로 묶인 관측치 모음."""

    label: str
    observations: List[Observation] = field(default_factory=list)

    @property
    def source_ids(self) -> List[str]:
        seen: List[str] = []
        for obs in self.observations:
            if obs.source_id not in seen:
                seen.append(obs.source_id)
        return seen


def extract_observations(sources: Sequence[SourceDoc]) -> List[Observation]:
    """출처에서 관측치를 추출한다."""
    observations: List[Observation] = []
    for source in sources:
        if source.error:
            continue
        if source.metrics:
            for metric in source.metrics:
                observations.append(
                    Observation(
                        label=metric.label,
                        value=metric.value,
                        raw_value=metric.raw_value,
                        unit=metric.unit,
                        period=metric.period,
                        source_id=source.source_id,
                        context=metric.context or "",
                    )
                )
            continue
        # 서사형 자료: 문장에서 수치 + 라벨 추출
        for metric in extract_metrics_from_text(
            source.raw_text, source_id=source.source_id, limit=15
        ):
            observations.append(
                Observation(
                    label=metric.label,
                    value=metric.value,
                    raw_value=metric.raw_value,
                    unit=metric.unit,
                    period=metric.period,
                    source_id=source.source_id,
                    context=metric.context or "",
                )
            )
    return observations


def group_observations(observations: Sequence[Observation]) -> List[ClaimGroup]:
    """라벨이 유사한 관측치를 하나의 그룹으로 묶는다."""
    groups: List[ClaimGroup] = []
    for obs in observations:
        placed = False
        for group in groups:
            if labels_match(group.label, obs.label, threshold=LABEL_MATCH_THRESHOLD):
                group.observations.append(obs)
                placed = True
                break
        if not placed:
            groups.append(ClaimGroup(label=obs.label, observations=[obs]))
    # 관측치 많은 그룹 우선
    groups.sort(key=lambda g: (-len(g.observations), g.label))
    return groups


def _period_key(obs: Observation) -> str:
    return (obs.period or "").strip()


def _unit_key(obs: Observation) -> str:
    return (obs.unit or "").strip().replace(" ", "")


def _values_agree(a: float, b: float, tolerance: float = DEFAULT_TOLERANCE) -> bool:
    if a == b:
        return True
    scale = max(abs(a), abs(b))
    if scale == 0:
        return True
    return abs(a - b) / scale <= tolerance


def _pick_primary(observations: Sequence[Observation]) -> Observation:
    """대표 관측치를 고른다.

    규칙: (단위, 기준기간) 조합이 가장 많이 등장한 쪽을 대표로 삼는다.
    소수 단위/기간의 관측치가 "확인된 값"으로 단정되는 것을 막기 위함이다.
    동률이면 가장 최근 기간 → 출처 ID 순으로 결정한다.
    """
    buckets: Dict[Tuple[str, str], List[Observation]] = {}
    for obs in observations:
        buckets.setdefault((_unit_key(obs), _period_key(obs)), []).append(obs)

    def bucket_rank(item: Tuple[Tuple[str, str], List[Observation]]) -> Tuple[int, str, str]:
        (unit, period), members = item
        return (len(members), period, unit)

    best_key, _ = max(buckets.items(), key=bucket_rank)
    pool = buckets[best_key]
    return max(pool, key=lambda o: (o.period or "", o.source_id))


def build_claims(
    sources: Sequence[SourceDoc],
    *,
    max_claims: int = 24,
    tolerance: float = DEFAULT_TOLERANCE,
) -> List[Claim]:
    """관측치를 근거/반대 근거가 구분된 주장으로 만든다."""
    observations = extract_observations(sources)
    groups = group_observations(observations)
    claims: List[Claim] = []

    for index, group in enumerate(groups, start=1):
        if len(claims) >= max_claims:
            break
        claim = _build_claim(group, index, tolerance)
        if claim is not None:
            claims.append(claim)

    claims.extend(_narrative_claims(sources, max_claims=max_claims - len(claims)))
    return claims


def _build_claim(group: ClaimGroup, index: int, tolerance: float) -> Optional[Claim]:
    obs_list = group.observations
    if not obs_list:
        return None
    primary = _pick_primary(obs_list)

    supporting: List[str] = []
    contradicting: List[str] = []
    incomparable: List[str] = []
    reasons: Dict[str, str] = {}

    for obs in obs_list:
        if obs.source_id == primary.source_id:
            supporting.append(obs.source_id)
            continue
        same_unit = _unit_key(obs) == _unit_key(primary)
        same_period = _period_key(obs) == _period_key(primary)
        if not same_unit:
            incomparable.append(obs.source_id)
            reasons[obs.source_id] = "unit"
            continue
        if not same_period:
            incomparable.append(obs.source_id)
            reasons[obs.source_id] = "period"
            continue
        if _values_agree(obs.value, primary.value, tolerance):
            supporting.append(obs.source_id)
        else:
            contradicting.append(obs.source_id)

    supporting = _dedup(supporting)
    contradicting = _dedup(contradicting)
    incomparable = _dedup(incomparable)

    unit_text = f" {primary.unit}" if primary.unit else ""
    period_text = f" ({primary.period})" if primary.period else ""
    text = (
        f"{primary.label} 의 확인된 값은 {primary.raw_value}{unit_text}{period_text} 입니다."
    )

    status = ClaimStatus.SUPPORTED
    if contradicting:
        status = ClaimStatus.CONTESTED
    elif not supporting:
        status = ClaimStatus.UNVERIFIED

    quotes: List[Quote] = []
    for obs in obs_list:
        if obs.source_id not in supporting and obs.source_id not in contradicting:
            continue
        text_quote = (obs.context or "").strip()
        if not text_quote:
            continue
        quotes.append(
            Quote(
                text=text_quote[:400],
                source_id=obs.source_id,
                verified=False,
                context=text_quote[:400],
            )
        )

    return Claim(
        claim_id=f"c{index:03d}",
        text=text,
        status=status,
        metrics=[
            Metric(
                label=obs.label,
                value=obs.value,
                raw_value=obs.raw_value,
                unit=obs.unit,
                period=obs.period,
                source_id=obs.source_id,
                context=obs.context,
            )
            for obs in obs_list
        ],
        supporting_source_ids=supporting,
        contradicting_source_ids=contradicting,
        incomparable_source_ids=incomparable,
        incomparable_reasons=reasons,
        quotes=quotes,
        origin="extracted",
    )


def _narrative_claims(sources: Sequence[SourceDoc], *, max_claims: int) -> List[Claim]:
    """서사형 자료에서 핵심 문장을 주장 후보로 만든다(근거 출처 1개)."""
    if max_claims <= 0:
        return []
    claims: List[Claim] = []
    index = len(claims)
    for source in sources:
        if source.metrics or source.error or not source.raw_text:
            continue
        for sentence in split_sentences(source.raw_text):
            if len(sentence) < 40 or len(sentence) > 320:
                continue
            if not any(ch.isdigit() for ch in sentence):
                continue
            index += 1
            claims.append(
                Claim(
                    claim_id=f"n{index:03d}",
                    text=sentence.strip(),
                    status=ClaimStatus.UNVERIFIED,
                    supporting_source_ids=[source.source_id],
                    quotes=[
                        Quote(
                            text=sentence.strip()[:400],
                            source_id=source.source_id,
                            verified=False,
                            context=sentence.strip()[:400],
                        )
                    ],
                    origin="extracted",
                )
            )
            if len(claims) >= max_claims:
                return claims
    return claims


def _extract_evidence_pairs(raw: Dict[str, Any]) -> List[Tuple[str, str]]:
    """LLM 응답에서 (source_id, quote) 쌍을 추출한다.

    관측된 모델별 응답 형태를 모두 지원한다.
      - 평면형 : {"text":..., "source_id":..., "quote":...}
      - 중첩형 : {"claim":..., "evidence":[{"source_id":..., "quote":...}]}
    """
    pairs: List[Tuple[str, str]] = []

    source_id = _first_value(raw, ("source_id", "sourceId", "source", "doc_id"))
    quote = _first_value(raw, ("quote", "citation", "excerpt", "evidence"))
    if source_id and quote:
        pairs.append((source_id, quote))

    for key in ("evidence", "citations", "sources", "quotes", "evidence_list"):
        items = raw.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            sid = _first_value(item, ("source_id", "sourceId", "source", "doc_id"))
            q = _first_value(item, ("quote", "citation", "excerpt", "text"))
            if sid and q:
                pairs.append((sid, q))
    return pairs


def merge_llm_claims(
    llm_claims: Sequence[Dict[str, Any]],
    sources: Sequence[SourceDoc],
    *,
    existing: Optional[List[Claim]] = None,
    max_claims: int = 24,
) -> List[Claim]:
    """LLM 이 만든 후보 주장을 검증 가능한 형태로 변환한다.

    - 인용문이 원문에 없으면 그 근거는 버린다(허위 근거 방지).
    - 수치가 원문에 없으면 해당 수치는 버린다.
    - 검증된 인용이 하나도 없으면 주장 전체를 버린다.
    """
    by_id = {s.source_id: s for s in sources}
    out: List[Claim] = list(existing or [])
    used_ids = {c.claim_id for c in out}

    for raw in llm_claims:
        if len(out) >= max_claims:
            break
        if not isinstance(raw, dict):
            continue
        text = _first_value(raw, ("text", "claim", "answer", "statement", "summary"))
        if not text:
            continue

        verified: List[Tuple[str, str]] = []
        for sid, quote_text in _extract_evidence_pairs(raw):
            source = by_id.get(sid)
            if source is None:
                log.info("LLM 이 가리킨 출처를 찾지 못해 근거를 버립니다: %s", sid)
                continue
            if not verify_quote(quote_text, source.raw_text):
                log.info(
                    "LLM 인용문이 원문과 일치하지 않아 근거를 버립니다: %s | 인용=%s",
                    sid,
                    quote_text[:80],
                )
                continue
            verified.append((sid, quote_text))

        if not verified:
            continue

        primary_sid, primary_quote = verified[0]
        primary_source = by_id[primary_sid]
        metrics: List[Metric] = []
        for m in raw.get("metrics") or raw.get("figures") or []:
            if not isinstance(m, dict):
                continue
            raw_value = _first_value(m, ("raw_value", "rawValue", "value", "text"))
            raw_value = str(raw_value or "").strip()
            if raw_value and raw_value not in primary_source.raw_text:
                continue
            try:
                value = float(str(m.get("value")).replace(",", ""))
            except (TypeError, ValueError):
                continue
            metrics.append(
                Metric(
                    label=str(
                        _first_value(m, ("label", "name", "metric")) or "확인 불가"
                    ).strip()
                    or "확인 불가",
                    value=value,
                    raw_value=raw_value or str(value),
                    unit=_first_value(m, ("unit", "units")),
                    period=_first_value(m, ("period", "date", "time")),
                    source_id=primary_sid,
                    context=primary_quote[:400],
                )
            )

        index = len(out) + 1
        claim_id = f"l{index:03d}"
        while claim_id in used_ids:
            index += 1
            claim_id = f"l{index:03d}"
        used_ids.add(claim_id)

        out.append(
            Claim(
                claim_id=claim_id,
                text=text,
                status=ClaimStatus.SUPPORTED,
                metrics=metrics,
                supporting_source_ids=_dedup([sid for sid, _ in verified]),
                quotes=[
                    Quote(
                        text=quote_text[:400],
                        source_id=sid,
                        verified=True,
                        context=quote_text[:400],
                    )
                    for sid, quote_text in verified
                ],
                origin="llm",
            )
        )
    return out


def _first_value(payload: Dict[str, Any], keys: Sequence[str]) -> str:
    """여러 후보 필드명 중 처음 값을 가진 것의 문자열 값을 돌려준다."""
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _dedup(items: List[str]) -> List[str]:
    seen: List[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
