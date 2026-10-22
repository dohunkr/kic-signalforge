"""LLM provider 추상화.

NVIDIA NIM 은 "모델 추론" 용도일 뿐 금융 데이터 공급원이 아니다.
금융 데이터는 반드시 connectors/ 에서 가져와야 한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from ..models import LlmUsage


class LlmProvider(ABC):
    """LLM 호출 인터페이스."""

    name: str = "base"

    @property
    @abstractmethod
    def available(self) -> bool:
        """키/연결 설정이 되어 실제로 호출 가능한 상태인지."""

    @abstractmethod
    def chat_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1200,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """JSON 객체를 반환하도록 유도한 채팅 호출.

        schema 를 주면 JSON Schema 로 구조를 강제한다(지원하는 제공기관 한정).
        실패하면 예외를 던진다(호출자는 결정론적 경로로 폴백).
        """

    @abstractmethod
    def describe(self) -> Dict[str, Any]:
        """상태 요약(키 값 미포함)."""

    def usage(self) -> LlmUsage:
        return LlmUsage(used=False, model=None)
