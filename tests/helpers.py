"""테스트 공통 헬퍼."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional

from app.connectors.base import Connector, ConnectorRequest, ConnectorResult
from app.models import ConnectorError, Metric, ProviderName, Provenance, SourceDoc


def make_source(
    source_id: str,
    *,
    provider: ProviderName = ProviderName.ECOS,
    title: str = "테스트 자료",
    url: str = "https://example.org/doc",
    provenance: Provenance = Provenance.LIVE,
    published_at: Optional[Any] = None,
    period_label: Optional[str] = "2025-12",
    unit: Optional[str] = "%",
    raw_text: str = "",
    metrics: Optional[List[Metric]] = None,
    error: Optional[str] = None,
    error_code: Optional[str] = None,
) -> SourceDoc:
    """테스트용 SourceDoc 생성."""
    now = datetime.now(timezone.utc)
    return SourceDoc(
        source_id=source_id,
        provider=provider,
        title=title,
        url=url,
        provenance=provenance,
        retrieved_at=now,
        published_at=published_at,
        period_label=period_label,
        unit=unit,
        excerpt=raw_text[:120],
        raw_text=raw_text,
        metrics=metrics or [],
        error=error,
        error_code=error_code,
    )


class FakeConnector(Connector):
    """테스트용 커넥터. 미리 정해진 결과를 돌려준다."""

    def __init__(
        self,
        provider: ProviderName,
        *,
        sources: Optional[List[SourceDoc]] = None,
        errors: Optional[List[ConnectorError]] = None,
        notes: Optional[List[str]] = None,
        raises: Optional[Exception] = None,
        configured: bool = True,
        mode: str = "live",
    ) -> None:
        super().__init__()
        self.provider = provider
        self._sources = sources or []
        self._errors = errors or []
        self._notes = notes or []
        self._raises = raises
        self._configured = configured
        self._mode = mode
        self.calls: List[ConnectorRequest] = []

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def mode(self) -> str:
        return self._mode

    def fetch(self, request: ConnectorRequest) -> ConnectorResult:
        self.calls.append(request)
        if self._raises is not None:
            raise self._raises
        return ConnectorResult(
            sources=list(self._sources), errors=list(self._errors), notes=list(self._notes)
        )
