"""대화형 온보딩 세션.

사용자 흐름
  1) 진입: "환영합니다. 무엇이 궁금하신가요?" (+ 예시 질문)
  2) 사용자 질문 입력 → "다음으로, 시뮬레이션에 사용할 투자 잔고는 얼마로 설정할까요?"
  3) 잔고 입력 → 조사 실행 → 결과(전망·확률·시뮬레이션) 전달

세션 상태는 SQLite 에 저장되므로 새로고침해도 이어서 진행할 수 있다.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..config import get_settings
from ..models import ChatMessage, ChatReply, ChatStep, ResearchRequest
from ..observability import get_logger
from ..service import ResearchService

log = get_logger(__name__)

#: 시작 화면에 보여줄 예시 질문
EXAMPLE_PROMPTS = [
    "2025년 삼성전자 반도체 업황 전망은 어때?",
    "원/달러 환율이 국내 수출 기업에 미치는 영향은?",
    "한국은행 기준금리 인상 시나리오의 근거는?",
    "국내 반도체 산업생산지수 흐름을 알려줘",
]

#: 잔고 최소값(이 미만은 입력 오류로 취급)
MIN_BALANCE = 10_000

#: 잔고 입력에서 숫자만 추출
_AMOUNT_RE = re.compile(r"([\d,]+)\s*(만|억|천만|조)?\s*원?")


def parse_amount(text: str) -> Optional[float]:
    """'1,000만원' '5000000' 같은 입력에서 금액(원)을 뽑는다."""
    if not text:
        return None
    match = _AMOUNT_RE.search(text.replace(" ", ""))
    if not match:
        return None
    raw = match.group(1).replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    unit = match.group(2) or ""
    multiplier = {"": 1, "만": 10_000, "천만": 10_000_000, "억": 100_000_000, "조": 1_000_000_000_000}[unit]
    return value * multiplier


class ChatSession:
    """대화 세션 하나의 상태."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.step = ChatStep.WELCOME
        self.question: str = ""
        self.company: str = ""
        self.period: str = ""
        self.balance: Optional[float] = None
        self.messages: List[ChatMessage] = []
        self.run_id: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "step": self.step.value,
            "question": self.question,
            "company": self.company,
            "period": self.period,
            "balance": self.balance,
            "run_id": self.run_id,
            "has_result": self.result is not None,
            "messages": [m.model_dump(mode="json") for m in self.messages],
        }


class ChatService:
    """대화 진행 + 조사 실행."""

    def __init__(self, service: Optional[ResearchService] = None) -> None:
        self._service = service or ResearchService()
        self._sessions: Dict[str, ChatSession] = {}

    # --- 세션 관리 -------------------------------------------------------
    def session(self, session_id: Optional[str] = None) -> ChatSession:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        new_id = session_id or uuid.uuid4().hex
        session = ChatSession(new_id)
        self._sessions[new_id] = session
        return session

    def reset(self, session_id: str) -> ChatSession:
        self._sessions.pop(session_id, None)
        return self.session(session_id)

    # --- 진행 ------------------------------------------------------------
    def welcome(self, session: ChatSession) -> ChatReply:
        session.step = ChatStep.WELCOME
        reply = (
            "환영합니다. 무엇이 궁금하신가요?\n"
            "관심 있는 기업이나 산업, 확인하고 싶은 근거를 편하게 물어보세요. "
            "공개 자료를 찾아 근거와 반대 근거를 함께 정리해 드립니다."
        )
        self._say(session, "assistant", reply)
        return ChatReply(
            session_id=session.session_id,
            step=session.step,
            reply=reply,
            quick_replies=EXAMPLE_PROMPTS,
            state=session.as_dict(),
        )

    def handle(self, session_id: Optional[str], message: str) -> ChatReply:
        """사용자 메시지를 처리하고 다음 단계를 돌려준다."""
        session = self.session(session_id)
        text = (message or "").strip()

        if session.step in (ChatStep.WELCOME, ChatStep.QUESTION):
            if not text:
                return self.welcome(session)
            self._say(session, "user", text)
            session.question = text
            session.company = self._service._guess_company(text)
            session.step = ChatStep.BALANCE
            reply = (
                "좋습니다. 그럼 이번 분석의 시뮬레이션에 사용할 **투자 잔고**는 얼마로 설정할까요?\n"
                "예) 1,000만원"
            )
            self._say(session, "assistant", reply)
            return ChatReply(
                session_id=session.session_id,
                step=session.step,
                reply=reply,
                quick_replies=["1,000만원", "5,000만원", "1억원"],
                state=session.as_dict(),
            )

        if session.step is ChatStep.BALANCE:
            self._say(session, "user", text)
            amount = parse_amount(text)
            if amount is None or amount < MIN_BALANCE:
                reply = (
                    "금액을 숫자로 인식하지 못했습니다. 예) 1,000만원 처럼 입력해 주세요."
                    if amount is None
                    else f"잔고가 너무 작게 입력되었습니다. {MIN_BALANCE:,}원 이상으로 입력해 주세요."
                )
                self._say(session, "assistant", reply)
                return ChatReply(
                    session_id=session.session_id,
                    step=session.step,
                    reply=reply,
                    quick_replies=["1,000만원", "5,000만원", "1억원"],
                    state=session.as_dict(),
                )
            session.balance = amount
            session.step = ChatStep.RUNNING
            reply = (
                f"확인했습니다. 잔고 {amount:,.0f}원을 기준으로 "
                "공개 자료를 조사하고 시뮬레이션을 계산하겠습니다. 잠시만 기다려 주세요."
            )
            self._say(session, "assistant", reply)
            result = self._run_research(session)
            session.step = ChatStep.DONE
            session.result = result
            done_reply = self._result_message(result)
            self._say(session, "assistant", done_reply)
            return ChatReply(
                session_id=session.session_id,
                step=session.step,
                reply=done_reply,
                quick_replies=["새 질문 하기", "브리핑 다운로드"],
                state={**session.as_dict(), "result": result},
            )

        # DONE 이후에는 새 질문을 시작한다.
        if text:
            session = self.reset(session.session_id)
            return self.handle(session.session_id, text)
        return self.welcome(session)

    # --- 조사 실행 -------------------------------------------------------
    def _run_research(self, session: ChatSession) -> Dict[str, Any]:
        briefing, simulation = self._service.run(
            question=session.question,
            company=session.company,
            period=session.period,
            balance=session.balance,
        )
        session.run_id = briefing.run_id
        payload: Dict[str, Any] = {
            "run_id": briefing.run_id,
            "briefing": briefing.model_dump(mode="json"),
        }
        if simulation is not None:
            payload["simulation"] = simulation.model_dump(mode="json")
        return payload

    def _result_message(self, result: Dict[str, Any]) -> str:
        briefing = result.get("briefing") or {}
        outlook = briefing.get("outlook") or {}
        sources = briefing.get("sources") or []
        claims = briefing.get("claims") or []
        simulation = result.get("simulation") or {}

        lines = ["조사 완료했습니다. 아래에서 근거와 전망을 확인하세요."]
        if outlook:
            lines.append(
                f"- 상승 확률 {outlook.get('probability_up', 0):.0%} / "
                f"하락 확률 {outlook.get('probability_down', 0):.0%} · "
                f"기대수익률 {outlook.get('expected_return', 0):+.2f}%"
            )
        lines.append(f"- 근거 출처 {len(sources)}건 · 확인된 주장 {len(claims)}건")
        if simulation:
            lines.append(
                f"- 시뮬레이션: {simulation.get('symbol')} "
                f"현재가 {simulation.get('current_price', 0):,.0f}"
                f"{simulation.get('currency', 'KRW')}"
            )
        return "\n".join(lines)

    def state_of(self, session_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """세션 현재 상태(메시지 + 결과)를 돌려준다(새로고침 복원용)."""
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return {
            **session.as_dict(),
            "result": session.result,
        }

    # --- 내부 ------------------------------------------------------------
    def _say(self, session: ChatSession, role: str, text: str) -> None:
        session.messages.append(
            ChatMessage(role=role, text=text, at=datetime.now(timezone.utc))
        )
