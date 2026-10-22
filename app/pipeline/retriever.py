"""Retriever - 허용된 커넥터에서 자료를 수집한다.

- 커넥터별 실패는 전체를 중단시키지 않는다(부분 실패 허용).
- 전체 실행 시간 상한(deadline)을 넘으면 이후 호출을 건너뛴다.
- 수집한 원문은 출처 메타데이터와 함께 보관한다(청크 + 메타데이터).
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..connectors.base import Connector, ConnectorRequest, ConnectorResult
from ..errors import ConnectorFailure
from ..models import ConnectorError as ConnectorErrorModel
from ..models import PlanItem, ProviderName, ResearchRequest, SourceDoc
from ..observability import Observability, get_logger

log = get_logger(__name__)


class Retriever:
    """_plan에 따라 커넥터를 호출하고 자료를 모은다."""

    def __init__(
        self,
        connectors: Dict[ProviderName, Connector],
        *,
        observability: Optional[Observability] = None,
        deadline: Optional[float] = None,
    ) -> None:
        self._connectors = connectors
        self._obs = observability or Observability()
        self._deadline = deadline

    @property
    def observability(self) -> Observability:
        return self._obs

    def retrieve(
        self, request: ResearchRequest, plan: Sequence[PlanItem]
    ) -> Tuple[List[SourceDoc], List[ConnectorErrorModel], List[str]]:
        sources: List[SourceDoc] = []
        errors: List[ConnectorErrorModel] = []
        notes: List[str] = []
        seen_ids: set = set()

        for item in plan:
            if self._expired():
                notes.append(
                    f"실행 시간 제한에 도달하여 '{item.provider.value}' 수집을 건너뛰었습니다."
                )
                break
            connector = self._connectors.get(item.provider)
            if connector is None:
                continue
            if not connector.configured:
                errors.append(
                    ConnectorErrorModel(
                        provider=item.provider,
                        code="unavailable",
                        message=f"{connector.describe().get('label', item.provider.value)} 설정이 없어 건너뛰었습니다.",
                        detail=connector.describe().get("note"),
                    )
                )
                continue

            connector_request = ConnectorRequest(
                query=item.sub_question,
                company=request.company,
                topic=request.topic,
                period=request.period or None,
                start_date=_period_to_date(request.period, "start"),
                end_date=_period_to_date(request.period, "end"),
                urls=request.ir_urls if item.provider is ProviderName.IR else (),
                keywords=item.keywords,
                max_items=20,
            )

            try:
                result: ConnectorResult = connector.fetch(connector_request)
            except ConnectorFailure as exc:
                errors.append(
                    ConnectorErrorModel(
                        provider=item.provider,
                        code=exc.code,
                        message=exc.message,
                        retryable=exc.retryable,
                        detail=exc.detail,
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 커넥터 버그가 전체를 죽이지 않도록
                errors.append(
                    ConnectorErrorModel(
                        provider=item.provider,
                        code="unknown",
                        message=f"커넥터 처리 중 예기치 않은 오류가 발생했습니다: {type(exc).__name__}",
                        detail=str(exc)[:200],
                    )
                )
                continue

            for err in result.errors:
                errors.append(err)
            notes.extend(result.notes)

            for source in result.sources:
                if source.source_id in seen_ids:
                    continue
                seen_ids.add(source.source_id)
                sources.append(source)

        return sources, errors, notes

    def _expired(self) -> bool:
        if self._deadline is None:
            return False
        return time.monotonic() >= self._deadline


def _period_to_date(period: Optional[str], which: str) -> Optional[str]:
    """'2025' / '2025-06' / '2025Q2' 같은 기간 표기를 시작/종료 날짜로 변환한다."""
    if not period:
        return None
    raw = period.strip()
    import re
    from datetime import date, timedelta

    m = re.search(r"(20\d{2})", raw)
    if not m:
        return None
    year = int(m.group(1))
    q = re.search(r"Q([1-4])", raw, re.I)
    if q:
        quarter = int(q.group(1))
        if which == "start":
            month = 3 * (quarter - 1) + 1
        else:
            month = 3 * quarter
        return date(year, month, 1).strftime("%Y%m%d")
    mm = re.search(r"(20\d{2})[-./](\d{1,2})", raw)
    if mm:
        month = int(mm.group(2))
        if which == "start":
            return date(year, month, 1).strftime("%Y%m%d")
        next_month = date(year + (month // 12), (month % 12) + 1, 1)
        return (next_month - timedelta(days=1)).strftime("%Y%m%d")
    if which == "start":
        return date(year, 1, 1).strftime("%Y%m%d")
    return date(year, 12, 31).strftime("%Y%m%d")
