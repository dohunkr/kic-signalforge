"""NVIDIA NIM provider adapter.

확인된 사실(2026-10-21 실측 + 공식 문서):
  * Base URL : https://integrate.api.nvidia.com/v1
  * Auth     : Authorization: Bearer $NVIDIA_API_KEY
  * /v1/models 는 인증 없이도 200 을 반환하며 "제공 가능한 모델 목록"을 준다.
  * **단, 목록에 있는 모델이 실제로 호출 가능한 것은 아니다.**
    계정에 프로비저닝되지 않은 모델은 404
    ("Function '<uuid>': Not found for account '<id>'") 를 반환한다.
    -> 실제 호출로 확인 가능한 모델만 후보로 삼는다.
  * 대부분의 추천 모델은 'reasoning 모델'이라 reasoning_content 를 먼저 소비한다.
    max_tokens 가 작으면 최종 content 가 None 이 된다.
    -> chat_template_kwargs.enable_thinking=false 로 추론을 끄고
       response_format=json_object 로 구조화된 출력을 받는다.
  * EOL 모델은 인증 검사 전에 410 을 돌려준다(모델 생존 확인에 활용).

키는 환경변수(NVIDIA_API_KEY)에서만 읽고, 로그/응답/프론트엔드에 노출하지 않는다.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import NimSettings
from ..errors import (
    ERR_INVALID_REQUEST,
    ERR_MODEL_UNAVAILABLE,
    ERR_QUOTA,
    ERR_SERVER,
    ERR_TIMEOUT,
    LlmError,
    LlmUnavailable,
    map_http_status,
)
from ..observability import get_logger
from .base import LlmProvider

log = get_logger(__name__)

#: 실제 호출로 확인된 모델 우선순위(2026-10-21, 표준 계정 기준).
#: 목록에 있더라도 계정 미제공이면 404 이므로, 호출 가능 여부를 별도로 확인한다.
PREFERRED_MODELS: List[str] = [
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "deepseek-ai/deepseek-v4.1-flash",
    "openai/gpt-oss-20b",
    "nvidia/nemotron-3-ultra-550b-a55b",
]

#: 추론(reasoning) 모델이 토큰 예산을 다 쓰지 않도록 thinking 을 끈다.
_NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}


class NimProvider(LlmProvider):
    """NVIDIA NIM (OpenAI 호환) 어댑터."""

    name = "nim"

    def __init__(self, settings: NimSettings, *, max_retries: int = 2) -> None:
        self._settings = settings
        self._max_retries = max_retries
        self._resolved_model: Optional[str] = None
        self._model_notes: List[str] = []
        self._rejected_models: List[str] = []
        self._calls = 0
        self._failures = 0

    # --- 상태 -----------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._settings.configured

    @property
    def model(self) -> str:
        return self._resolved_model or self._settings.model

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "configured": self.available,
            "base_url": self._settings.base_url,
            "model": self.model,
            "notes": list(self._model_notes),
        }

    def usage(self):
        from ..models import LlmUsage

        return LlmUsage(
            used=self._calls > 0 and self._failures < self._calls,
            model=self.model if self._calls else None,
            reason=(
                None
                if self._calls
                else "LLM 호출 없음(키 미설정 또는 결정론적 경로만 사용)"
            ),
            calls=self._calls,
            failures=self._failures,
            notes=list(self._model_notes),
        )

    # --- 모델 검증 ------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }

    def _list_models(self) -> List[str]:
        url = f"{self._settings.base_url.rstrip('/')}/models"
        try:
            with httpx.Client(timeout=min(self._settings.timeout_seconds, 20.0)) as client:
                resp = client.get(url, headers=self._headers())
            if resp.status_code != 200:
                self._model_notes.append(
                    f"모델 목록 확인 실패(HTTP {resp.status_code}). 설정된 모델을 시도합니다."
                )
                return []
            data = resp.json()
            return [m.get("id") for m in data.get("data", []) if m.get("id")]
        except Exception as exc:  # noqa: BLE001
            self._model_notes.append(
                f"모델 목록 확인 실패: {type(exc).__name__}. 설정된 모델을 사용합니다."
            )
            return []

    def _probe_model(self, model: str) -> Tuple[bool, str]:
        """실제로 호출 가능한지 최소 요청으로 확인한다."""
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 4,
            "temperature": 0,
            **_NO_THINKING,
        }
        try:
            # Probe 는 연결성만 확인하므로 무거운 모델도 여기서 오래 기다리지 않는다.
            with httpx.Client(timeout=min(self._settings.timeout_seconds, 6.0)) as client:
                resp = client.post(
                    f"{self._settings.base_url.rstrip('/')}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.TimeoutException:
            return False, "타임아웃"
        except Exception as exc:  # noqa: BLE001
            return False, type(exc).__name__

        if resp.status_code == 200:
            return True, ""
        if resp.status_code == 404:
            return False, "계정에 프로비저닝되지 않은 모델"
        if resp.status_code == 401:
            return False, "인증 실패"
        if resp.status_code == 410:
            return False, "서비스 종료(EOL)"
        return False, f"HTTP {resp.status_code}"

    def resolve_model(self) -> Optional[str]:
        """실제로 호출 가능한 모델을 찾아 확정한다."""
        if not self.available:
            return None
        if self._resolved_model:
            return self._resolved_model

        # /models 전체 목록에는 수십~수백 개가 포함될 수 있다. 이를 전부 순차 probe
        # 하면 Vercel 무료 함수 제한에 걸리므로, 설정 모델과 짧은 대체 후보만 확인한다.
        candidates: List[str] = []
        if self._settings.model and self._settings.model not in self._rejected_models:
            candidates.append(self._settings.model)
        for candidate in PREFERRED_MODELS:
            if candidate not in candidates and candidate not in self._rejected_models:
                candidates.append(candidate)

        if not candidates:
            candidates = [self._settings.model or PREFERRED_MODELS[0]]

        for candidate in candidates[:4]:
            ok, reason = self._probe_model(candidate)
            if ok:
                if candidate != self._settings.model:
                    self._model_notes.append(
                        f"설정된 모델 '{self._settings.model}' 을 사용할 수 없어 "
                        f"'{candidate}' 로 대체했습니다."
                    )
                self._resolved_model = candidate
                return candidate
            self._rejected_models.append(candidate)
            self._model_notes.append(f"'{candidate}' 사용 불가: {reason}")

        self._model_notes.append(
            "호출 가능한 모델을 찾지 못했습니다. NIM 계정 권한/모델 상태를 확인하십시오."
        )
        return None

    # --- 호출 -----------------------------------------------------------
    def chat_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1200,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.available:
            raise LlmUnavailable(
                "NVIDIA_API_KEY가 설정되지 않아 LLM을 호출할 수 없습니다.",
                provider=self.name,
                detail="키 발급: https://build.nvidia.com/settings",
            )

        model = self.resolve_model()
        if model is None:
            raise LlmError(
                ERR_MODEL_UNAVAILABLE,
                "호출 가능한 NVIDIA NIM 모델을 찾지 못했습니다.",
                provider=self.name,
                detail="; ".join(self._model_notes[-3:]),
            )

        last_error: Optional[Exception] = None
        budget = max_tokens
        # max_retries는 추가 재시도 횟수이므로 0이면 정확히 한 번 호출한다.
        for attempt in range(1, self._max_retries + 2):
            try:
                payload = self._build_payload(model, system, user, budget, schema)
                with httpx.Client(timeout=self._settings.timeout_seconds) as client:
                    resp = client.post(
                        f"{self._settings.base_url.rstrip('/')}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    )
                if resp.status_code == 200:
                    return self._handle_success(resp.json())
                if resp.status_code == 404:
                    # 계정 미제공 모델이면 다음 후보로 넘어간다.
                    detail = ""
                    try:
                        detail = resp.json().get("detail", "")
                    except Exception:  # noqa: BLE001
                        detail = resp.text[:120]
                    self._model_notes.append(f"'{model}' 호출 불가: {detail[:100]}")
                    self._rejected_models.append(model)
                    self._resolved_model = None
                    model = self.resolve_model()
                    if model is None:
                        raise LlmError(
                            ERR_MODEL_UNAVAILABLE,
                            "호출 가능한 NVIDIA NIM 모델을 찾지 못했습니다.",
                            provider=self.name,
                            detail="; ".join(self._model_notes[-3:]),
                        )
                    continue

                code, message = map_http_status(resp.status_code, self.name)
                if code in (ERR_QUOTA, ERR_SERVER, ERR_TIMEOUT) and attempt <= self._max_retries:
                    time.sleep(min(2 ** attempt * 0.5, 4.0))
                    continue
                self._failures += 1
                raise LlmError(
                    code,
                    message,
                    provider=self.name,
                    detail=f"HTTP {resp.status_code}",
                    retryable=False,
                )
            except LlmError as exc:
                # 추론이 예산을 다 쓴 경우에는 예산을 늘려 다시 시도한다.
                if exc.retryable and attempt <= self._max_retries:
                    budget = min(int(budget * 2), 8192)
                    self._model_notes.append(
                        f"응답이 비어 있어 출력 토큰 예산을 {budget} 으로 늘려 재시도합니다."
                    )
                    continue
                raise
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt <= self._max_retries:
                    time.sleep(min(2 ** attempt * 0.5, 4.0))
                    continue
                self._failures += 1
                raise LlmError(
                    ERR_TIMEOUT,
                    "NIM 호출 시간이 초과되었습니다.",
                    provider=self.name,
                    detail=type(exc).__name__,
                ) from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt <= self._max_retries:
                    time.sleep(min(2 ** attempt * 0.5, 4.0))
                    continue
                self._failures += 1
                raise LlmError(
                    "network",
                    "NIM 네트워크 연결에 실패했습니다.",
                    provider=self.name,
                    detail=type(exc).__name__,
                ) from exc
            except Exception as exc:  # noqa: BLE001
                self._failures += 1
                raise LlmError(
                    ERR_SERVER,
                    "NIM 호출 중 예기치 않은 오류가 발생했습니다.",
                    provider=self.name,
                    detail=type(exc).__name__,
                ) from exc

        self._failures += 1
        raise LlmError(
            ERR_SERVER,
            "NIM 호출이 최종 실패했습니다.",
            provider=self.name,
            detail=type(last_error).__name__ if last_error else None,
        )

    def _build_payload(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
            **_NO_THINKING,
        }
        if schema:
            # JSON Schema 로 구조를 강제한다(NVIDIA NIM 지원 확인).
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema},
            }
        else:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _handle_success(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self._calls += 1
        choices = data.get("choices") or []
        if not choices:
            raise LlmError(ERR_INVALID_REQUEST, "LLM 응답에 choices가 없습니다.", provider=self.name)
        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is None:
            # reasoning 이 예산을 모두 사용한 경우다. 다음 시도에서 예산을 늘린다.
            raise LlmError(
                ERR_INVALID_REQUEST,
                "LLM이 최종 응답을 생성하지 못했습니다(추론 예산 초과).",
                provider=self.name,
                detail=f"reasoning={bool(message.get('reasoning_content'))}",
                retryable=True,
            )
        return self._parse_json(content)

    # --- 파싱 -----------------------------------------------------------
    @staticmethod
    def _parse_json(content: str) -> Dict[str, Any]:
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        text = text.strip()
        if not text:
            raise LlmError(ERR_INVALID_REQUEST, "LLM이 빈 응답을 반환했습니다.", provider="nim")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end == -1 or end <= start:
                # 배열만 반환한 경우 대비
                start, end = text.find("["), text.rfind("]")
                if start == -1 or end == -1 or end <= start:
                    raise LlmError(
                        ERR_INVALID_REQUEST,
                        "LLM이 JSON을 반환하지 않았습니다.",
                        provider="nim",
                        retryable=True,
                    )
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                # 출력 토큰 한도 때문에 JSON 이 잘렸을 수 있다 -> 재시도 가능 처리
                raise LlmError(
                    ERR_INVALID_REQUEST,
                    "LLM이 파싱 가능한 JSON을 반환하지 않았습니다.",
                    provider="nim",
                    retryable=True,
                ) from exc
        if isinstance(parsed, list):
            parsed = {"claims": parsed}
        if not isinstance(parsed, dict):
            raise LlmError(ERR_INVALID_REQUEST, "LLM 응답이 JSON 객체가 아닙니다.", provider="nim")
        return parsed

    def check_model_alive(self, model: Optional[str] = None) -> Dict[str, Any]:
        """모델이 아직 제공되는지 확인한다(410=EOL, 401=인증만 실패=생존).

        인증 실패(401)와 EOL(410)을 구분하기 위해 의도적으로 키 없이 호출한다.
        """
        target = model or self._settings.model
        url = f"{self._settings.base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": target,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(url, headers={"Content-Type": "application/json"}, json=payload)
        except Exception as exc:  # noqa: BLE001
            return {"model": target, "status": "unknown", "detail": type(exc).__name__}
        if resp.status_code == 410:
            return {"model": target, "status": "eol", "detail": resp.text[:200]}
        if resp.status_code == 401:
            return {"model": target, "status": "alive", "detail": "인증만 실패 (키 필요)"}
        return {"model": target, "status": "unknown", "detail": f"HTTP {resp.status_code}"}


__all__ = ["NimProvider", "PREFERRED_MODELS", "ERR_MODEL_UNAVAILABLE"]
