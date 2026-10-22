"""리서치 서비스 - 조사 + 시세 + 시뮬레이션을 한 번에 제공.

UI/채팅 흐름은 이 서비스를 통해서만 동작한다.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import AppSettings, get_settings
from .connectors.dart import DartConnector
from .connectors.http_client import HttpClient
from .connectors.price import PriceConnector
from .errors import ConnectorFailure
from .models import (
    Briefing,
    OutlookSummary,
    PricePoint,
    ResearchRequest,
    SimulationResult,
)
from .observability import get_logger
from .pipeline.orchestrator import Orchestrator
from .pipeline import outlook as outlook_mod
from .pipeline.simulation import build as build_simulation
from .pipeline.simulation import expected_moves
from .storage.sqlite_store import SessionStore

log = get_logger(__name__)

#: 질문에서 기업명을 뽑아내는 보조 표현
_COMPANY_SUFFIXES = ("주식회사", "㈜", "(주)", "지주", "홀딩스", "그룹")


class ResearchService:
    """조사 실행 → 시세 확보 → 전망/시뮬레이션."""

    def __init__(
        self,
        settings: Optional[AppSettings] = None,
        *,
        orchestrator: Optional[Orchestrator] = None,
        store: Optional[SessionStore] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.orchestrator = orchestrator or Orchestrator(self.settings, store=store)
        self._http = HttpClient(
            timeout=20.0,
            max_retries=self.settings.max_retries,
            observability=self.orchestrator.observability,
        )
        self._price = PriceConnector(self.settings.price, self._http)

    # --- 기업/종목 해석 --------------------------------------------------
    def _dart_connector(self) -> Optional[DartConnector]:
        for connector in self.orchestrator._connectors.values():
            if isinstance(connector, DartConnector):
                return connector
        return None

    def resolve_company(self, company: str, question: str = "") -> Tuple[str, Optional[str]]:
        """기업명 → (표시명, 종목코드)."""
        name = (company or "").strip()
        if not name and question:
            name = self._guess_company(question)
        if not name:
            return "", None
        connector = self._dart_connector()
        if connector is None or not connector.configured:
            return name, None
        try:
            stock_code = connector._resolve_stock_code(name)
        except Exception as exc:  # noqa: BLE001
            log.info("종목코드 해석 실패(무시): %s", type(exc).__name__)
            stock_code = None
        return name, stock_code

    def _guess_company(self, question: str) -> str:
        """질문에서 기업명을 찾는다.

        우선순위:
          1) DART 기업코드 목록에 실제로 존재하는 이름(상장사)
          2) 그래도 없으면 일반 단어 후보
        """
        connector = self._dart_connector()
        if connector is not None and connector.configured:
            try:
                found = connector.find_companies(question)
            except Exception as exc:  # noqa: BLE001
                log.info("기업 자동 인식 실패(무시): %s", type(exc).__name__)
                found = []
            if found:
                return found[0]

        tokens = re.findall(r"[가-힣A-Za-z0-9]{2,10}", question or "")
        for token in tokens:
            cleaned = token
            for suffix in _COMPANY_SUFFIXES:
                cleaned = cleaned.replace(suffix, "")
            if cleaned.isdigit() or re.fullmatch(r"20[0-9]{2}년?", cleaned):
                continue
            if len(cleaned) >= 2:
                return cleaned
        return ""

    # --- 실행 ------------------------------------------------------------
    def run(
        self,
        *,
        question: str,
                company: str = "",
        topic: str = "",
        period: str = "",
        ir_urls: Optional[Sequence[str]] = None,
        balance: Optional[float] = None,
    ) -> Tuple[Briefing, Optional[SimulationResult]]:
        """조사를 실행하고 시뮬레이션까지 만든다."""
        resolved_name, stock_code = self.resolve_company(company, question)
        request = ResearchRequest(
            question=question,
            topic=topic,
            company=resolved_name,
            period=period,
            ir_urls=list(ir_urls or []),
        )
        briefing = self.orchestrator.run(request)

        simulation = self._build_simulation(
            briefing,
            stock_code=stock_code,
            company=resolved_name,
            balance=balance,
        )
        if simulation is not None:
            briefing.disclaimer = (
                briefing.disclaimer
                + " "
                + simulation.disclaimer
            )
        return briefing, simulation

    def simulate(
        self,
        *,
        company: str = "",
        question: str = "",
        balance: Optional[float] = None,
        outlook: Optional[OutlookSummary] = None,
    ) -> Optional[SimulationResult]:
        """조사 결과 없이 시뮬레이션만 만들 때 사용(진단용·재사용)."""
        resolved_name, stock_code = self.resolve_company(company, question)
        if not stock_code:
            return None
        symbol = self._price.resolve_symbol(stock_code)
        if not symbol:
            return None
        try:
            points, meta = self._price.fetch_series(symbol)
        except ConnectorFailure:
            return None
        history: List[PricePoint] = points[-250:]
        up_move, down_move = expected_moves(history)
        summary = outlook or outlook_mod.summarize([], [], [])
        summary = summary.model_copy(
            update={
                "expected_return": round(
                    summary.probability_up * up_move
                    - summary.probability_down * down_move,
                    2,
                )
            }
        )
        return build_simulation(
            history,
            summary,
            symbol=symbol,
            company=resolved_name or meta.get("longName") or symbol,
            currency=meta.get("currency", "KRW"),
            balance=balance,
            data_source="공개 시세 API (Yahoo Finance)",
        )

    def _build_simulation(
        self,
        briefing: Briefing,
        *,
        stock_code: Optional[str],
        company: str,
        balance: Optional[float],
    ) -> Optional[SimulationResult]:
        if not self.settings.price.enabled or not stock_code:
            return None
        symbol = self._price.resolve_symbol(stock_code)
        if not symbol:
            return None
        try:
            points, meta = self._price.fetch_series(symbol)
        except ConnectorFailure as exc:
            log.info("시세 조회 실패(무시): %s", exc.message)
            return None

        # 시뮬레이션용: 최근 1년 종가
        history: List[PricePoint] = points[-250:]

        # 기대수익률은 과거 수익률 분포 + 확률로 다시 계산한다(시뮬레이션과 일치시킨다).
        up_move, down_move = expected_moves(history)
        if briefing.outlook is not None:
            refreshed = outlook_mod.summarize(
                briefing.claim_outlooks,
                briefing.claims,
                briefing.uncertainties,
                expected_up=up_move,
                expected_down=down_move,
            )
            briefing.outlook = refreshed

        return build_simulation(
            history,
            briefing.outlook,
            symbol=symbol,
            company=company or meta.get("longName") or symbol,
            currency=meta.get("currency", "KRW"),
            balance=balance,
            data_source="공개 시세 API (Yahoo Finance)",
        )
