"""데이터 커넥터 공통 인터페이스.

각 커넥터는 서로 독립 모듈이며, 실패해도 나머지 파이프라인은 계속 동작해야 한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence

from ..models import ConnectorError, ProviderName, SourceDoc


class Connector(ABC):
    """공통 데이터 커넥터."""

    provider: ProviderName
    #: 이 커넥터가 수집할 수 있는 구체적인 단위(예: "공시 목록", "통계 시계열")
    kind: str = "documents"
    #: 사용자 화면에 표시되는 이름
    label: str = "데이터 커넥터"

    def __init__(self, *, max_retries: int = 2, timeout: float = 20.0) -> None:
        self.max_retries = max_retries
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        """키 등 실행에 필요한 설정이 갖춰졌는지."""
        return True

    @property
    def mode(self) -> str:
        """live | sample | unavailable"""
        return "live"

    @abstractmethod
    def fetch(self, request: "ConnectorRequest") -> "ConnectorResult":
        """자료를 수집한다. 실패는 예외가 아니라 ConnectorResult.errors 로 반환한다."""

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.provider.value,
            "label": self.label,
            "kind": self.kind,
            "configured": self.configured,
            "mode": self.mode,
        }


class ConnectorRequest:
    """커넥터 호출 요청."""

    def __init__(
        self,
        *,
        query: str = "",
        company: str = "",
        topic: str = "",
        period: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        urls: Sequence[str] = (),
        keywords: Sequence[str] = (),
        max_items: int = 20,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.query = query
        self.company = company
        self.topic = topic
        self.period = period
        self.start_date = start_date
        self.end_date = end_date
        self.urls = list(urls)
        self.keywords = list(keywords)
        self.max_items = max_items
        self.extra = extra or {}


class ConnectorResult:
    """커넥터 호출 결과."""

    def __init__(
        self,
        *,
        sources: Optional[List[SourceDoc]] = None,
        errors: Optional[List[ConnectorError]] = None,
        notes: Optional[List[str]] = None,
    ) -> None:
        self.sources: List[SourceDoc] = sources or []
        self.errors: List[ConnectorError] = errors or []
        self.notes: List[str] = notes or []

    @property
    def ok(self) -> bool:
        return not self.errors or bool(self.sources)

    def merge(self, other: "ConnectorResult") -> "ConnectorResult":
        self.sources.extend(other.sources)
        self.errors.extend(other.errors)
        self.notes.extend(other.notes)
        return self

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ConnectorResult(sources={len(self.sources)}, errors={len(self.errors)}, "
            f"notes={len(self.notes)})"
        )
