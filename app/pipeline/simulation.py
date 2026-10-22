"""주식 시뮬레이션 - 과거 실제 주가 + AI 근거 기반 시나리오.

원칙
  * 과거 구간은 **실제 종가**를 그대로 사용한다(왜곡하지 않는다).
  * 미래 구간은 과거 변동성에서 뽑은 3개 시나리오(상승/기준/하락)로 제시한다.
  * 각 시나리오의 확률은 근거 기반 전망(OutlookSummary)에서 나온다.
  * 차트 각 점에는 '그 시점의 전망'과 '그 근거'를 붙여 hover 로 보여준다.
  * 수익률은 과거 수익률 분포에서 계산하며 방법을 함께 공개한다.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..models import (
    OutlookSummary,
    PricePoint,
    SimulationPoint,
    SimulationResult,
    SimulationScenario,
)

#: 시나리오 색상(Design.md 팔레트: Primary/Success/Error)
SCENARIO_COLORS = {
    "bull": "#10B981",  # Success
    "base": "#6366F1",  # Primary
    "bear": "#EF4444",  # Error
}

#: 미래 구간(개월)
FORECAST_MONTHS = 12


def _log_returns(points: Sequence[PricePoint]) -> List[float]:
    out: List[float] = []
    for prev, cur in zip(points, points[1:]):
        if prev.close > 0 and cur.close > 0:
            out.append(math.log(cur.close / prev.close))
    return out


def _monthly_returns(points: Sequence[PricePoint], window: int = 21) -> List[float]:
    """거래일 window 개(≈1개월) 기준 수익률 분포."""
    out: List[float] = []
    for index in range(window, len(points)):
        base = points[index - window].close
        if base > 0:
            out.append((points[index].close - base) / base)
    return out


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return ordered[low]
    weight = pos - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _deterministic_noise(seed_text: str, count: int) -> List[float]:
    """같은 입력에서 항상 같은 경로가 나오도록 한 결정론적 잡음 시퀀스."""
    seed = 0
    for char in seed_text:
        seed = (seed * 131 + ord(char)) % (2**31 - 1)
    out: List[float] = []
    state = seed or 1
    for _ in range(count):
        state = (state * 1103515245 + 12345) % (2**31 - 1)
        out.append((state / (2**31 - 1)) * 2.0 - 1.0)
    return out


def _tilt_to_target(
    points: Sequence[PricePoint],
    *,
    target_final: float,
    last_price: float,
) -> List[PricePoint]:
    """경로의 모양은 유지한 채 최종값만 목표치로 맞춘다.

    시나리오의 '기대 결과'는 이미 정해져 있으므로(기대수익률 ± 과거 수익률 폭)
    경로가 거기에 정확히 도달하도록 로그 배수를 선형으로 얹는다.
    """
    if not points or last_price <= 0 or target_final <= 0:
        return list(points)
    current = points[-1].close
    if current <= 0:
        return list(points)
    premium = math.log(target_final / current)
    months = len(points)
    out: List[PricePoint] = []
    for index, point in enumerate(points):
        factor = math.exp(premium * (index + 1) / max(1, months))
        out.append(PricePoint(date=point.date, close=round(point.close * factor, 2)))
    return out


def _center(values: Sequence[float]) -> List[float]:
    """평균을 0으로 맞춘다(기준 경로가 기대수익률에 수렴하도록)."""
    if not values:
        return []
    mean = sum(values) / len(values)
    return [v - mean for v in values]


def _month_labels(count: int, start: date) -> List[str]:
    labels: List[str] = []
    year, month = start.year, start.month
    for _ in range(count):
        labels.append(f"{year}-{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return labels


def _scenario_points(
    base_price: float,
    total_return_pct: float,
    monthly_sigma: float,
    labels: Sequence[str],
    noise: Sequence[float],
) -> List[PricePoint]:
    """시나리오 경로.

    - 드리프트는 '근거가 시사하는 총 수익률'에서만 가져온다.
    - 변동성은 과거 변동성에서만 가져온다.
    - 잡음은 평균 0이 되도록 중앙화해 기준 경로가 기대수익률에 수렴하게 한다.
    """
    points: List[PricePoint] = []
    price = base_price
    monthly_drift = (total_return_pct / 100.0) / max(1, len(labels))
    for index, label in enumerate(labels):
        z = max(-2.0, min(2.0, noise[index]))
        price = max(1.0, price * (1.0 + monthly_drift + monthly_sigma * z))
        points.append(PricePoint(date=label, close=round(price, 2)))
    return points


def expected_moves(history: Sequence[PricePoint]) -> Tuple[float, float]:
    """과거 1개월 수익률 분포에서 기대 상승/하락 폭(%)을 뽑는다.

    상승은 상위 25% 수준, 하락은 하위 10% 수준(최근 12개월 중 가장 나빴던 구간)을 쓴다.
    하위 25% 는 강세장에서도 양수가 될 수 있어 그대로 쓰면 낙폭을 과소평가한다.
    """
    returns = _monthly_returns(history)
    if not returns:
        return 0.0, 0.0
    up = max(0.0, _percentile(returns, 0.75)) * 100.0
    down = max(0.0, -_percentile(returns, 0.10)) * 100.0
    if down <= 0:
        # 표본에 하락한 달이 없다면, 과거 월 변동폭의 절반을 낙폭 기준으로 쓴다.
        mean_abs = sum(abs(r) for r in returns) / len(returns)
        down = round(max(mean_abs * 100.0 * 0.5, 1.0), 2)
    return round(up, 2), round(down, 2)


def build(
    history: Sequence[PricePoint],
    outlook: OutlookSummary,
    *,
    symbol: Optional[str] = None,
    company: Optional[str] = None,
    currency: str = "KRW",
    balance: Optional[float] = None,
    months: int = FORECAST_MONTHS,
    data_source: str = "",
) -> SimulationResult:
    """과거 실제 주가와 근거 기반 전망으로 시뮬레이션 결과를 만든다."""
    now = datetime.now(timezone.utc)
    notes: List[str] = []
    if not history:
        return SimulationResult(
            symbol=symbol,
            company=company,
            currency=currency,
            balance=balance,
            generated_at=now,
            data_source=data_source,
            notes=["시세 데이터를 확보하지 못해 시뮬레이션을 만들지 못했습니다."],
        )

    closes = [p.close for p in history]
    last_price = closes[-1]

    log_rets = _log_returns(history)
    daily_sigma = statistics.pstdev(log_rets) if len(log_rets) > 1 else 0.0
    monthly_sigma = daily_sigma * math.sqrt(21)

    window_returns = _monthly_returns(history)

    prob_up = outlook.probability_up if outlook else 0.5
    prob_down = outlook.probability_down if outlook else 0.5

    # 기대 폭은 과거 수익률 분포에서, 드리프트는 근거가 시사하는 기대수익률에서만 가져온다.
    up_move, down_move = expected_moves(history)
    expected_return_pct = outlook.expected_return if outlook else 0.0
    # 상승 시나리오 = 과거 상위 25% 수준, 하락 시나리오 = 과거 하위 10% 수준,
    # 기준 시나리오 = 근거가 시사하는 기대수익률(범위를 벗어나면 안쪽으로 보정).
    bull_total = up_move
    bear_total = -down_move
    base_total = min(max(expected_return_pct, bear_total), bull_total)
    # 세 경로가 서로 다른 값에 위치하도록 최소 간격을 둔다.
    epsilon = max(0.5, (bull_total - bear_total) * 0.02)
    if bull_total - base_total < epsilon:
        base_total = bull_total - epsilon
    if base_total - bear_total < epsilon:
        bear_total = base_total - epsilon

    start = date.today()
    labels = _month_labels(months, start)
    seed = f"{symbol or company or 'signalforge'}"
    noise_bull = _center(_deterministic_noise(seed + "|bull", months))
    noise_base = _center(_deterministic_noise(seed + "|base", months))
    noise_bear = _center(_deterministic_noise(seed + "|bear", months))

    def _final_for(total_pct: float) -> float:
        return max(1.0, last_price * (1.0 + total_pct / 100.0))

    base_points = _tilt_to_target(
        _scenario_points(last_price, base_total, monthly_sigma, labels, noise_base),
        target_final=_final_for(base_total),
        last_price=last_price,
    )
    bull_points = _tilt_to_target(
        _scenario_points(last_price, bull_total, monthly_sigma, labels, noise_bull),
        target_final=_final_for(bull_total),
        last_price=last_price,
    )
    bear_points = _tilt_to_target(
        _scenario_points(last_price, bear_total, monthly_sigma, labels, noise_bear),
        target_final=_final_for(bear_total),
        last_price=last_price,
    )

    scenarios = [
        SimulationScenario(
            key="bull",
            label="상승 시나리오",
            color=SCENARIO_COLORS["bull"],
            points=bull_points,
            probability=prob_up,
            description="근거가 지지하는 주장이 우세하고 수익성 개선 기대가 높은 경우",
        ),
        SimulationScenario(
            key="base",
            label="기준 시나리오",
            color=SCENARIO_COLORS["base"],
            points=base_points,
            probability=1.0 - prob_up - prob_down if outlook else 1.0,
            description="근거가 균형을 이루며 과거 변동성 범위 안에서 움직이는 경우",
        ),
        SimulationScenario(
            key="bear",
            label="하락 시나리오",
            color=SCENARIO_COLORS["bear"],
            points=bear_points,
            probability=prob_down,
            description="반대 근거가 우세하거나 위험 요인이 앞서는 경우",
        ),
    ]
    for scenario in scenarios:
        if scenario.points:
            scenario.final_price = scenario.points[-1].close
            scenario.return_pct = round(
                (scenario.final_price - last_price) / last_price * 100.0, 2
            )
            scenario.probability = round(scenario.probability, 4)

    # hover 용 포인트: 기준 시나리오를 메인으로 삼고 근거를 붙인다.
    bull_evidence = (outlook.bull_evidence if outlook else [])[:3]
    bear_evidence = (outlook.bear_evidence if outlook else [])[:3]
    outlook_points: List[SimulationPoint] = []
    base = scenarios[1]
    for index, point in enumerate(base.points):
        cumulative = round((point.close - last_price) / last_price * 100.0, 2)
        direction = "상승" if cumulative >= 0 else "하락"
        evidence = bull_evidence if cumulative >= 0 else bear_evidence
        detail_bits = [
            f"{direction} {abs(cumulative):.1f}%",
            f"상승 확률 {prob_up:.0%} / 하락 확률 {prob_down:.0%}",
        ]
        if outlook and outlook.confidence:
            detail_bits.append(f"근거 신뢰도 {outlook.confidence:.0%}")
        outlook_points.append(
            SimulationPoint(
                date=point.date,
                price=point.close,
                cumulative_return=cumulative,
                outlook=f"{direction} · 확률 {prob_up:.0%}",
                detail=" · ".join(detail_bits),
                evidence=evidence,
                probability=prob_up if cumulative >= 0 else prob_down,
            )
        )

    notes.append(
        f"과거 {len(history)}거래일의 실제 종가를 사용했고, 월 변동성은 "
        f"{monthly_sigma * 100:.2f}% 로 계산했습니다."
    )
    if window_returns:
        notes.append(
            f"기대수익률은 과거 1개월 수익률 분포의 상위 25%({up_move:.1f}%) / "
            f"하위 10%({down_move:.1f}%) 와 상승/하락 확률을 결합해 계산했습니다."
        )
    else:
        notes.append("과거 수익률 표본이 부족해 기대수익률은 0% 로 두었습니다.")
        up_move = down_move = 0.0

    return SimulationResult(
        symbol=symbol,
        company=company,
        currency=currency,
        current_price=round(last_price, 2),
        history=list(history),
        scenarios=scenarios,
        outlook_points=outlook_points,
        outlook=outlook,
        balance=balance,
        generated_at=now,
        data_source=data_source or "공개 시세 API(Yahoo Finance)",
        notes=notes,
    )
