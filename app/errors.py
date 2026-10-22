"""커넥터/LLM 오류 분류.

사용자에게 "왜 실패했는지"를 투명하게 보여주기 위한 표준 오류 taxonomy.
각 제공기관의 실제 오류 코드(문서/실측 기준)를 그대로 매핑한다.
"""

from __future__ import annotations

from typing import Optional

# --- 표준 오류 코드 -----------------------------------------------------
ERR_TIMEOUT = "timeout"
ERR_QUOTA = "quota"            # 호출한도 초과
ERR_AUTH = "auth"              # 키 없음/잘못됨/사용 불가
ERR_NOT_FOUND = "not_found"    # 데이터 없음
ERR_HTTP = "http_error"        # 그 외 HTTP 오류
ERR_SERVER = "server_error"    # 5xx
ERR_NETWORK = "network"        # 연결 실패/DNS
ERR_PARSE = "parse"            # 응답 파싱 실패
ERR_INVALID_REQUEST = "invalid_request"
ERR_MODEL_EOL = "model_unavailable"
ERR_MODEL_UNAVAILABLE = "model_unavailable"
ERR_UNKNOWN = "unknown"

#: 재시도 가능한 오류 코드
RETRYABLE = {ERR_TIMEOUT, ERR_SERVER, ERR_NETWORK}


class SignalForgeError(Exception):
    """모든 커넥터/LLM 오류의 베이스."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        provider: str = "unknown",
        detail: Optional[str] = None,
        retryable: Optional[bool] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider = provider
        self.detail = detail
        self.retryable = RETRYABLE.__contains__(code) if retryable is None else retryable

    def __str__(self) -> str:  # pragma: no cover - 표시용
        return f"[{self.provider}/{self.code}] {self.message}"


class ConnectorFailure(SignalForgeError):
    """데이터 커넥터가 발생시킨 예외(raised). 사용자에게 보여줄 기록은 models.ConnectorError."""


class LlmError(SignalForgeError):
    """LLM provider 실패."""


class LlmUnavailable(SignalForgeError):
    """LLM provider를 사용할 수 없음(키 미설정 등).

    이 경우 파이프라인은 결정론적(규칙 기반) 경로로 계속 진행해야 한다.
    """

    def __init__(self, message: str, *, provider: str = "nim", detail: Optional[str] = None) -> None:
        super().__init__(
            ERR_AUTH,
            message,
            provider=provider,
            detail=detail,
            retryable=False,
        )


# --- 제공기관별 실제 오류 코드 매핑 --------------------------------------

#: 금융감독원 OpenDART 응답 status 코드 (공식 개발가이드 기준)
DART_STATUS_MAP = {
    "000": (None, "정상"),
    "010": (ERR_AUTH, "등록되지 않은 인증키입니다."),
    "011": (ERR_AUTH, "일시 정지된 인증키입니다."),
    "012": (ERR_AUTH, "사용이 허가되지 않은 IP입니다."),
    "013": (ERR_NOT_FOUND, "조회할 데이터가 없습니다."),
    "014": (ERR_NOT_FOUND, "파일이 존재하지 않습니다."),
    "020": (ERR_QUOTA, "호출 제한을 초과하였습니다."),
    "021": (ERR_QUOTA, "동시 조회 가능한 기업 수(100개)를 초과하였습니다."),
    "100": (ERR_INVALID_REQUEST, "요청 값이 필드 정의에 맞지 않습니다."),
    "101": (ERR_INVALID_REQUEST, "부적절한 접근입니다."),
    "800": (ERR_SERVER, "점검 중입니다."),
    "900": (ERR_UNKNOWN, "정의되지 않은 오류가 발생하였습니다."),
    "901": (ERR_AUTH, "개인정보 보유기간 만료로 사용할 수 없는 키입니다."),
}

#: 한국은행 ECOS 응답 RESULT.CODE
ECOS_CODE_MAP = {
    "ERROR-100": (ERR_INVALID_REQUEST, "필수 값이 누락되었거나 형식이 올바르지 않습니다."),
    "ERROR-101": (ERR_NOT_FOUND, "지정된 통계를 찾을 수 없습니다. 통계 코드/항목 코드/날짜 형식을 확인하십시오."),
    "ERROR-102": (ERR_INVALID_REQUEST, "요청 값이 올바르지 않습니다."),
    "ERROR-103": (ERR_AUTH, "인증키가 유효하지 않습니다."),
    "ERROR-104": (ERR_QUOTA, "일일 호출한도를 초과하였습니다."),
    "ERROR-301": (
        ERR_INVALID_REQUEST,
        "조회건수 값이 유효하지 않습니다. 샘플 키는 1회 10건까지만 호출할 수 있습니다.",
    ),
}

#: 국가데이터처 KOSIS 응답 err 코드
KOSIS_ERR_MAP = {
    "10": (ERR_AUTH, "인증키가 없거나 유효하지 않습니다."),
    "11": (ERR_AUTH, "인증KEY가 유효하지 않습니다."),
    "12": (ERR_INVALID_REQUEST, "요청 파라미터의 값이나 형식이 올바르지 않습니다."),
    "20": (ERR_INVALID_REQUEST, "요청변수의 값이 잘못되었습니다. (통계표마다 요구 파라미터가 다릅니다.)"),
    "30": (ERR_NOT_FOUND, "결과가 존재하지 않습니다. (통계코드·조사주기·기간을 확인하십시오.)"),
    "31": (ERR_AUTH, "인증키 사용 기한이 만료되었습니다."),
    "40": (ERR_QUOTA, "호출 제한을 초과하였습니다."),
    "41": (ERR_QUOTA, "1회 호출 결과 허용 셀 수를 초과하였습니다."),
}

#: NVIDIA NIM HTTP 상태 코드
NIM_STATUS_MAP = {
    400: (ERR_INVALID_REQUEST, "요청이 잘못되었습니다."),
    401: (ERR_AUTH, "인증 실패: NVIDIA_API_KEY를 확인하십시오."),
    403: (ERR_AUTH, "접근이 거부되었습니다."),
    404: (ERR_NOT_FOUND, "요청한 엔드포인트/모델을 찾을 수 없습니다."),
    408: (ERR_TIMEOUT, "요청 시간이 초과되었습니다."),
    410: (ERR_MODEL_EOL, "요청한 모델이 서비스 종료(EOL)되었습니다."),
    422: (ERR_INVALID_REQUEST, "요청 값을 처리할 수 없습니다."),
    429: (ERR_QUOTA, "요청 할당량을 초과하였습니다."),
    500: (ERR_SERVER, "서버 내부 오류가 발생하였습니다."),
    502: (ERR_SERVER, "게이트웨이 오류가 발생하였습니다."),
    503: (ERR_SERVER, "서비스가 일시적으로 불가합니다."),
    504: (ERR_TIMEOUT, "게이트웨이 타임아웃이 발생하였습니다."),
}


def map_http_status(status: int, provider: str) -> tuple[str, str]:
    """HTTP 상태 코드 -> (표준 코드, 사용자 메시지)."""
    if status in NIM_STATUS_MAP:
        return NIM_STATUS_MAP[status]
    if 500 <= status < 600:
        return ERR_SERVER, "업스트림 서버 오류가 발생하였습니다."
    if 400 <= status < 500:
        return ERR_INVALID_REQUEST, "요청이 거부되었습니다."
    return ERR_UNKNOWN, "알 수 없는 HTTP 오류가 발생하였습니다."


def map_dart_status(status: str) -> tuple[str, str]:
    code, msg = DART_STATUS_MAP.get(status, (ERR_UNKNOWN, "알 수 없는 응답 코드입니다."))
    return code, msg


def map_ecos_code(code: str) -> tuple[str, str]:
    mapped = ECOS_CODE_MAP.get(code)
    if mapped:
        return mapped
    return ERR_UNKNOWN, "알 수 없는 응답 코드입니다."


def map_kosis_err(code: str) -> tuple[str, str]:
    mapped = KOSIS_ERR_MAP.get(code)
    if mapped:
        return mapped
    return ERR_UNKNOWN, "알 수 없는 응답 코드입니다."
