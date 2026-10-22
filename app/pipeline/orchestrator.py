"""Orchestrator - 전체 파이프라인 실행과 종료 조건 관리.

실행 흐름
  Planner -> Retriever -> Parser(파싱은 커넥터/claims 내부) -> Claim Extractor
  -> Evidence Checker -> Report Writer

종료 조건
  * 전체 wall-clock 상한(KIC_MAX_RUN_SECONDS)
  * 커넥터별 재시도 횟수 상한
  * 수집 자료/주장 개수 상한
"""

from __future__ import annotations

import time
import uuid
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..config import AppSettings, get_settings
from ..connectors.base import Connector
from ..connectors.dart import DartConnector
from ..connectors.ecos import EcosConnector
from ..connectors.http_client import HttpClient
from ..connectors.ir import IrConnector
from ..connectors.kosis import KosisConnector
from ..errors import ConnectorFailure, LlmUnavailable
from ..llm.base import LlmProvider
from ..llm.nim import NimProvider
from ..models import (
    Briefing,
    ClaimStatus,
    ConnectorError as ConnectorErrorModel,
    LlmUsage,
    PlanItem,
    Provenance,
    ProviderName,
    ResearchRequest,
    RunStats,
    RunStatus,
    SourceDoc,
)
from ..observability import Observability, get_logger
from ..storage.sqlite_store import SessionStore
from . import claims as claim_mod
from . import evidence as evidence_mod
from . import outlook as outlook_mod
from . import planner as planner_mod
from . import report as report_mod
from .retriever import Retriever

log = get_logger(__name__)

#: LLM 에 넘기는 원문 총 길이 상한(문자)
LLM_CONTEXT_CHAR_LIMIT = 14000

#: LLM 이 만들 수 있는 후보 주장 수 상한
LLM_MAX_CANDIDATES = 8

#: LLM 에 넘기는 최대 출처 수
LLM_MAX_SOURCES = 12

#: 프롬프트에 넣는 출력 예시(모델이 형식을 정확히 따르도록 돕는다)
_EXAMPLE_CLAIM = (
    '{"claims":[{"text":"주장 문장","source_id":"출처ID",'
    '"quote":"원문에서 그대로 복사한 구절",'
    '"metrics":[{"label":"지표명","value":1.0,"raw_value":"1.0",'
    '"unit":"%","period":"2025-12"}]}]}'
)

#: LLM 출력 구조를 강제하는 JSON Schema (NVIDIA NIM 의 json_schema 사용)
CLAIMS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "주장 문장"},
                    "source_id": {
                        "type": "string",
                        "description": "인용한 출처의 출처ID (반드시 주어진 값 중 하나)",
                    },
                    "quote": {
                        "type": "string",
                        "description": "원문에서 그대로 복사한 연속 구절",
                    },
                    "metrics": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "value": {"type": "number"},
                                "raw_value": {"type": "string"},
                                "unit": {"type": "string"},
                                "period": {"type": "string"},
                            },
                            "required": ["label", "value", "raw_value", "unit", "period"],
                        },
                    },
                },
                "required": ["text", "source_id", "quote"],
            },
        }
    },
    "required": ["claims"],
}


class Orchestrator:
    """리서치 1회를 실행한다."""

    def __init__(
        self,
        settings: Optional[AppSettings] = None,
        *,
        llm: Optional[LlmProvider] = None,
        connectors: Optional[Dict[ProviderName, Connector]] = None,
        store: Optional[SessionStore] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm if llm is not None else NimProvider(self.settings.nim, max_retries=self.settings.max_retries)
        self.store = store
        self.observability = Observability()
        self._connectors = connectors if connectors is not None else self._build_connectors()

    # --- 커넥터 ---------------------------------------------------------
    def _build_connectors(self) -> Dict[ProviderName, Connector]:
        is_vercel = os.environ.get("VERCEL") == "1"
        http = HttpClient(
            timeout=7.0 if is_vercel else 20.0,
            max_retries=self.settings.max_retries,
            observability=self.observability,
        )
        return {
            ProviderName.DART: DartConnector(
                self.settings.dart,
                http,
                cache_dir=self.settings.data_dir,
                fetch_bodies=1 if is_vercel else 3,
            ),
            ProviderName.ECOS: EcosConnector(self.settings.ecos, http),
            ProviderName.KOSIS: KosisConnector(self.settings.kosis, http),
            ProviderName.IR: IrConnector(http),
        }

    # --- 실행 -----------------------------------------------------------
    def run(self, request: ResearchRequest, *, run_id: Optional[str] = None) -> Briefing:
        run_id = run_id or uuid.uuid4().hex
        started = datetime.now(timezone.utc)
        start_monotonic = time.monotonic()
        deadline = start_monotonic + self.settings.max_run_seconds

        plan: List[PlanItem] = planner_mod.plan(request)

        retriever = Retriever(
            self._connectors, observability=self.observability, deadline=deadline
        )
        sources, errors, notes = retriever.retrieve(request, plan)

        claims = claim_mod.build_claims(sources)
        llm_usage = LlmUsage(used=False)

        if self.llm is not None and self.llm.available:
            llm_claims, llm_usage = self._llm_assist(request, sources, claims)
            if llm_claims:
                claims = claim_mod.merge_llm_claims(
                    llm_claims, sources, existing=claims, max_claims=32
                )
        else:
            llm_usage = LlmUsage(
                used=False,
                reason=(
                    "NVIDIA_API_KEY 미설정으로 LLM을 사용하지 않았습니다. "
                    "결정론적(규칙 기반) 추출만 수행했습니다."
                ),
            )

        checked_claims, uncertainties = evidence_mod.check(
            claims,
            sources,
            stale_after_days=self.settings.stale_after_days,
            today=started.date(),
        )

        # 6-1) 주장별 방향성 판정 → 상승/하락 확률
        claim_outlooks, outlook_notes = outlook_mod.classify_with_llm(
            checked_claims, llm=self.llm
        )
        if not claim_outlooks:
            claim_outlooks = outlook_mod.classify_deterministically(checked_claims)
            if outlook_notes:
                llm_usage.notes.extend(outlook_notes)
        outlook_summary = outlook_mod.summarize(claim_outlooks, checked_claims, uncertainties)

        gaps = report_mod.data_gaps_from(checked_claims, errors, sources)
        status = _decide_status(checked_claims, sources, errors)

        finished = datetime.now(timezone.utc)
        stats = RunStats(
            started_at=started,
            finished_at=finished,
            duration_seconds=round(time.monotonic() - start_monotonic, 2),
            retries_used=self.observability.retries_used,
            http_calls=self.observability.http_calls,
            sources_collected=len(sources),
            providers_attempted=sorted({p.provider.value for p in plan}),
        )

        briefing = Briefing(
            run_id=run_id,
            question=request.question,
            topic=request.topic or request.company,
            period=request.period or None,
            status=status,
            plan=plan,
            sources=sources,
            claims=checked_claims,
            uncertainties=uncertainties,
            errors=errors,
            data_gaps=gaps,
            llm=llm_usage,
            outlook=outlook_summary,
            claim_outlooks=claim_outlooks,
            stats=stats,
        )
        briefing.report_markdown = report_mod.render(briefing)

        if self.store is not None:
            try:
                self.store.save_briefing(briefing)
            except Exception as exc:  # noqa: BLE001 - 저장 실패가 결과 자체를 망가뜨리면 안 된다
                log.warning("브리핑 저장 실패(무시): %s", type(exc).__name__)

        log.info(
            "리서치 완료 run_id=%s 상태=%s 출처=%d 주장=%d 소요=%.1fs",
            run_id,
            status.value,
            len(sources),
            len(checked_claims),
            stats.duration_seconds,
        )
        return briefing

    # --- LLM 보조 -------------------------------------------------------
    def _llm_assist(
        self,
        request: ResearchRequest,
        sources: Sequence[SourceDoc],
        existing: Sequence[Any],
    ) -> tuple[List[Dict[str, Any]], LlmUsage]:
        usage = LlmUsage(used=False, model=None)
        context = _build_llm_context(sources)
        if not context:
            usage.reason = "LLM에 전달할 원문 컨텍스트가 비어 있어 호출을 생략했습니다."
            return [], usage

        system = (
            "당신은 금융 리서치 보조 에이전트입니다. 반드시 주어진 원문에서만 인용하십시오. "
            "원문에 없는 수치나 문장을 만들지 마십시오. "
            "quote 필드에는 원문 본문에서 문자 그대로 연속한 구절을 복사하십시오. "
            "줄바꿈을 바꾸거나 말줄임을 넣지 마십시오."
        )
        source_ids = ", ".join(sorted({s.source_id for s in sources}))
        user = (
            f"질문: {request.question}\n"
            f"주제: {request.topic or request.company or '-'}\n"
            f"기간: {request.period or '-'}\n"
            f"사용 가능한 출처ID: {source_ids}\n\n"
            "원문에서 확인되는 수치와 사실을 그대로 인용해 최대한 추출하십시오. "
            "원문에 있는 내용은 인용해도 됩니다.\n"
            "출력 형식(JSON):\n"
            f"{_EXAMPLE_CLAIM}\n"
            "- source_id 는 위 목록에 있는 값 중 하나를 그대로 사용하십시오.\n"
            "- quote 는 해당 출처의 '본문:' 부분에서 핵심 구절만(최대 3줄) 글자 그대로 복사하십시오.\n"
            "- metrics 는 원문에 실제로 등장한 수치만 넣되 **최대 3개**만 넣고, "
            "없으면 빈 배열([])을 넣으십시오.\n"
            "- claims 는 최대 5개까지만 출력하십시오.\n"
            "- text 는 원문에 있는 사실만 서술하십시오.\n\n"
            f"{context}"
        )

        candidates: List[Dict[str, Any]] = []
        payload: Dict[str, Any] = {}
        # 금융 근거 요약은 짧은 구조화 출력이면 충분하다. 긴 생성은 Serverless timeout을 유발한다.
        max_tokens = 1200 if self.settings.max_retries == 0 else 2000
        max_attempts = 1 if self.settings.max_retries == 0 else 3
        for attempt in range(1, max_attempts + 1):
            try:
                payload = self.llm.chat_json(system, user, max_tokens=max_tokens)
            except LlmUnavailable as exc:
                usage.reason = exc.message
                usage.notes.append("LLM 사용 불가: 키가 설정되지 않았습니다.")
                return [], usage
            except Exception as exc:  # noqa: BLE001
                usage.reason = f"LLM 호출 실패: {type(exc).__name__}"
                usage.notes.append(f"{type(exc).__name__}: {exc}")
                return [], usage

            usage.calls += 1
            raw_claims = payload.get("claims")
            if not isinstance(raw_claims, list):
                raw_claims = payload.get("items") if isinstance(payload.get("items"), list) else []
            candidates = [
                c for c in raw_claims[:LLM_MAX_CANDIDATES] if isinstance(c, dict)
            ]
            if candidates:
                break
            # 0건이면 더 구체적으로 다시 지시한다.
            usage.notes.append("LLM이 주장을 하나도 반환하지 않아 더 구체적으로 재시도합니다.")
            user = (
                user
                + "\n\n다시 시도합니다: 본문에 등장하는 수치 중 가장 중요한 것 하나만 골라 "
                "주장 1개를 반드시 만들어서 반환하십시오. "
                "metrics 는 1개, quote 는 1줄만 넣으십시오."
            )

        usage.used = bool(candidates)
        usage.model = getattr(self.llm, "model", None)
        if not candidates:
            usage.reason = "LLM이 검증 가능한 후보 주장을 반환하지 않았습니다."
        return candidates, usage


def _build_llm_context(sources: Sequence[SourceDoc]) -> str:
    """LLM 에 넘길 원문 컨텍스트(길이 제한 적용).

    수치가 담긴 구조화 자료 우선으로 배치하고, 출처 수를 제한해
    지연시간과 잡음을 함께 줄인다.
    """
    ordered = sorted(
        sources,
        key=lambda s: (0 if s.metrics else 1, s.source_id),
    )[:LLM_MAX_SOURCES]
    blocks: List[str] = []
    used = 0
    for source in ordered:
        if source.error or not source.raw_text:
            continue
        remaining = LLM_CONTEXT_CHAR_LIMIT - used
        if remaining <= 400:
            break
        body = source.raw_text[: remaining - 200]
        used += len(body)
        blocks.append(
            "\n".join(
                [
                    f"### 출처ID: {source.source_id}",
                    f"제목: {source.title}",
                    f"URL: {source.url}",
                    f"발행일: {source.published_at.isoformat() if source.published_at else '미확인'}",
                    f"기준기간: {source.period_label or '미확인'} / 단위: {source.unit or '미확인'}",
                    f"구분: {source.provenance.label}",
                    "본문:",
                    body,
                    "",
                ]
            )
        )
    return "".join(blocks)


def _decide_status(
    claims: Sequence[Any], sources: Sequence[SourceDoc], errors: Sequence[ConnectorErrorModel]
) -> RunStatus:
    if not sources:
        return RunStatus.INSUFFICIENT_EVIDENCE
    supported = [c for c in claims if c.status is ClaimStatus.SUPPORTED]
    contested = [c for c in claims if c.status is ClaimStatus.CONTESTED]
    if not supported and not contested:
        return RunStatus.INSUFFICIENT_EVIDENCE
    if errors:
        return RunStatus.PARTIAL
    return RunStatus.COMPLETED
