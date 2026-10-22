"""FastAPI 라우트."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..chat.session import ChatService
from ..config import get_settings
from ..llm.nim import NimProvider
from ..models import Briefing, ChatRequest, ResearchRequest
from ..observability import get_logger
from ..pipeline.orchestrator import Orchestrator
from ..service import ResearchService
from ..storage.sqlite_store import SessionStore

log = get_logger(__name__)

router = APIRouter()

_store: Optional[SessionStore] = None
_orchestrator: Optional[Orchestrator] = None
_service: Optional[ResearchService] = None
_chat: Optional[ChatService] = None


def get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore(get_settings().sqlite_path)
    return _store


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator(get_settings(), store=get_store())
    return _orchestrator


def get_service() -> ResearchService:
    global _service
    if _service is None:
        _service = ResearchService(get_settings(), orchestrator=get_orchestrator())
    return _service


def get_chat() -> ChatService:
    global _chat
    if _chat is None:
        _chat = ChatService(get_service())
    return _chat


def reset_services() -> None:
    """캐시된 서비스 인스턴스를 비운다(테스트·핫리로드용)."""
    global _store, _orchestrator, _service, _chat
    _store = None
    _orchestrator = None
    _service = None
    _chat = None


@router.get("/api/health")
def health() -> Dict[str, Any]:
    """헬스체크 + 커넥터 설정 상태(키 값 자체는 절대 노출하지 않는다)."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": "KIC SignalForge",
        "version": "0.2.0",
        "build": (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "local")[:7],
        "connectors": settings.connector_status(),
        "limits": {
            "max_run_seconds": settings.max_run_seconds,
            "max_retries": settings.max_retries,
            "stale_after_days": settings.stale_after_days,
        },
    }


@router.post("/api/research")
def research(request: ResearchRequest) -> Dict[str, Any]:
    """리서치 1회 실행(+ 가능하면 시뮬레이션)."""
    service = get_service()
    started = time.monotonic()
    try:
        briefing, simulation = service.run(
            question=request.question,
            company=request.company,
            topic=request.topic,
            period=request.period,
            ir_urls=request.ir_urls,
            balance=request.balance,
        )
    except Exception as exc:  # noqa: BLE001 - 서버가 죽지 않도록 명시적 오류 반환
        log.exception("리서치 실행 실패")
        raise HTTPException(
            status_code=500,
            detail=f"리서치 실행 중 오류가 발생했습니다: {type(exc).__name__}",
        ) from exc
    payload = briefing.model_dump(mode="json")
    if simulation is not None:
        payload["simulation"] = simulation.model_dump(mode="json")
    payload["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return payload


class RedTeamRequest(BaseModel):
    question: str
    outlook: Dict[str, Any] = Field(default_factory=dict)
    claims: List[Dict[str, Any]] = Field(default_factory=list)
    sources: List[Dict[str, Any]] = Field(default_factory=list)


@router.post("/api/red-team")
def red_team(request: RedTeamRequest) -> Dict[str, Any]:
    """검증된 분석 자료 안에서 반대 논거를 별도 호출로 점검한다."""
    provider = NimProvider(get_settings().nim, max_retries=get_settings().max_retries)
    if not provider.available:
        return {"available": False, "note": "AI 설정이 없어 원문에 기록된 반대 근거만 확인할 수 있습니다."}
    claims = request.claims[:20]
    sources = request.sources[:30]
    allowed = {str(s.get("source_id")) for s in sources}
    system = (
        "당신은 투자 전망의 반대편을 심사하는 독립 검토자입니다. 주어진 검증된 주장과 출처만 사용하고 "
        "낙관 전망을 가장 강하게 반박하세요. 자료에 없는 사실은 만들지 말고 부족하면 명시하세요. "
        "결론이 기준 전망과 다르면 이를 좌우한 출처 ID를 지정하세요. JSON 객체만 반환하세요."
    )
    user = json.dumps({"question": request.question, "baseline": request.outlook,
                       "claims": claims, "sources": sources}, ensure_ascii=False)
    try:
        result = provider.chat_json(system, user, max_tokens=900)
        cited = [str(x) for x in result.get("source_ids", []) if str(x) in allowed]
        return {"available": True, "review": str(result.get("review") or result.get("summary") or "반대 심사 결과를 만들지 못했습니다.")[:1200],
                "changed_conclusion": bool(result.get("changed_conclusion", False)),
                "source_ids": cited, "model": provider.model}
    except Exception as exc:  # noqa: BLE001
        log.warning("반대 심사 실패: %s", type(exc).__name__)
        return {"available": False, "note": "반대 심사 AI 호출에 실패했습니다. 기존 반대 근거를 아래에서 확인해 주세요."}


@router.post("/api/chat")
def chat(request: ChatRequest) -> Dict[str, Any]:
    """대화형 온보딩: 질문 → 잔고 → 조사 결과."""
    chat_service = get_chat()
    try:
        reply = chat_service.handle(request.session_id, request.message)
    except Exception as exc:  # noqa: BLE001
        log.exception("대화 처리 실패")
        raise HTTPException(
            status_code=500,
            detail=f"대화 처리 중 오류가 발생했습니다: {type(exc).__name__}",
        ) from exc
    return reply.model_dump(mode="json")


@router.post("/api/chat/reset")
def chat_reset(request: ChatRequest) -> Dict[str, Any]:
    """대화 세션을 처음부터 다시 시작한다."""
    chat_service = get_chat()
    session = chat_service.reset(request.session_id or "")
    reply = chat_service.welcome(session)
    return reply.model_dump(mode="json")


class SimulateRequest(BaseModel):
    """시뮬레이션 단독 요청."""

    company: str = ""
    question: str = ""
    balance: Optional[float] = None


@router.post("/api/simulate")
def simulate(request: SimulateRequest) -> Dict[str, Any]:
    """조사 결과 없이 시뮬레이션만 계산한다(진단·재사용)."""
    service = get_service()
    try:
        simulation = service.simulate(
            company=request.company,
            question=request.question,
            balance=request.balance,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("시뮬레이션 실패")
        raise HTTPException(
            status_code=500,
            detail=f"시뮬레이션 계산 중 오류가 발생했습니다: {type(exc).__name__}",
        ) from exc
    if simulation is None:
        return {"simulation": None, "note": "종목을 식별하지 못해 시뮬레이션을 만들지 못했습니다."}
    return {"simulation": simulation.model_dump(mode="json")}


@router.get("/api/chat/state")
def chat_state(session_id: str = Query(..., min_length=1)) -> Dict[str, Any]:
    """세션 상태를 반환한다(새로고침 후 복원용)."""
    state = get_chat().state_of(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    return state


@router.get("/api/runs")
def list_runs(limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    return {"runs": get_store().list_runs(limit)}


@router.get("/api/runs/{run_id}")
def get_run(run_id: str) -> Dict[str, Any]:
    payload = get_store().get_briefing(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="해당 run_id 를 찾을 수 없습니다.")
    return payload


@router.get("/api/runs/{run_id}/report.md", response_class=PlainTextResponse)
def get_report_markdown(run_id: str) -> str:
    payload = get_store().get_briefing(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="해당 run_id 를 찾을 수 없습니다.")
    return payload.get("report_markdown", "")
