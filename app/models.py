"""KIC SignalForge - 도메인 데이터 모델.

설계 원칙
  * 모든 근거는 ``SourceDoc`` 이고, 출처 ID/URL/발행일/기준기간/단위를 반드시 갖는다.
  * ``Provenance`` 로 "실제 API 결과"와 "샘플/데모 데이터"를 구조적으로 구분한다.
  * LLM이 만든 수치/인용은 ``Claim`` 에 담기 전 검증 단계(evidence.py)를 통과해야 한다.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Provenance(str, Enum):
    """자료의 출처 신뢰도 구분."""

    LIVE = "live"        # 실제 공식 API / 공개 공식 페이지에서 실시간 수집
    SAMPLE = "sample"    # 제공된 샘플 데이터(공식 샘플 키 또는 번들 샘플 코퍼스)
    DEMO = "demo"        # 시연용 고정 시드 데이터(실제 시장 데이터 아님)

    @property
    def is_real(self) -> bool:
        return self is Provenance.LIVE

    @property
    def label(self) -> str:
        return {
            Provenance.LIVE: "실제 API/공식 자료",
            Provenance.SAMPLE: "샘플 데이터(실제 아님)",
            Provenance.DEMO: "데모 시드 데이터(실제 아님)",
        }[self]


class UncertaintyKind(str, Enum):
    STALENESS = "staleness"              # 오래된 문서
    MISSING_DATE = "missing_date"        # 발행일 불명
    CONFLICT = "conflict"                # 같은 지표/기간인데 값이 다름
    UNIT_MISMATCH = "unit_mismatch"      # 단위가 달라 직접 비교 불가
    PERIOD_MISMATCH = "period_mismatch"  # 기준기간이 달라 직접 비교 불가
    MISSING_SOURCE = "missing_source"    # 근거 없음
    API_ERROR = "api_error"              # API 실패/타임아웃/쿼터
    COVERAGE_GAP = "coverage_gap"        # 자료 공백
    UNVERIFIED_QUOTE = "unverified_quote"  # 원문 대조 실패
    AMBIGUITY = "ambiguity"              # 해석이 여러 갈래
    LLM_UNAVAILABLE = "llm_unavailable"  # LLM 미사용으로 한계


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ProviderName(str, Enum):
    DART = "dart"
    ECOS = "ecos"
    KOSIS = "kosis"
    IR = "ir"
    PRICE = "price"
    SAMPLE = "sample"


class Metric(BaseModel):
    """주장/자료에 포함된 개별 수치."""

    label: str = Field(..., description="지표 이름 (예: 한국은행 기준금리, 영업이익)")
    value: float
    raw_value: str = Field(..., description="원문 그대로의 값 표기")
    unit: Optional[str] = None
    period: Optional[str] = Field(None, description="기준 기간 (예: 2025-12, 2025Q3, 2025-12-31)")
    source_id: Optional[str] = None
    context: Optional[str] = Field(None, description="이 수치가 등장한 원문 문장")

    @field_validator("label")
    @classmethod
    def _label_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("label must not be blank")
        return v

    @field_validator("raw_value")
    @classmethod
    def _raw_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("raw_value must not be blank")
        return v


class Quote(BaseModel):
    """원문에서 실제로 추출된 인용 구절."""

    text: str
    source_id: str
    verified: bool = False,  # 원문 정규화 부분 문자열 대조 결과
    context: Optional[str] = None


class SourceDoc(BaseModel):
    """검색/파싱된 자료 1건."""

    source_id: str
    provider: ProviderName
    title: str
    url: str
    provenance: Provenance
    retrieved_at: datetime
    published_at: Optional[date] = None
    period_label: Optional[str] = None
    unit: Optional[str] = None
    excerpt: str = ""
    raw_text: str = ""
    metrics: List[Metric] = Field(default_factory=list)
    publisher: Optional[str] = None
    doc_id: Optional[str] = None
    language: str = "ko"
    extra: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = Field(None, description="수집 실패 사유(문서 자체가 실패한 경우)")
    error_code: Optional[str] = None

    @field_validator("url")
    @classmethod
    def _url_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("url must not be blank")
        return v

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("title must not be blank")
        return v


class Uncertainty(BaseModel):
    """불확실성/경고 항목."""

    kind: UncertaintyKind
    severity: Severity = Severity.WARNING
    message: str
    source_ids: List[str] = Field(default_factory=list)
    metric_label: Optional[str] = None


class ClaimStatus(str, Enum):
    SUPPORTED = "supported"      # 근거로 뒷받침됨
    CONTESTED = "contested"      # 반대 근거가 존재
    UNVERIFIED = "unverified"    # 근거 부족


class Claim(BaseModel):
    """검증된 핵심 주장 1건."""

    claim_id: str
    text: str
    status: ClaimStatus = ClaimStatus.UNVERIFIED
    metrics: List[Metric] = Field(default_factory=list)
    supporting_source_ids: List[str] = Field(default_factory=list)
    contradicting_source_ids: List[str] = Field(default_factory=list)
    incomparable_source_ids: List[str] = Field(
        default_factory=list, description="단위/기간이 달라 직접 비교하지 않은 출처"
    )
    incomparable_reasons: Dict[str, str] = Field(
        default_factory=dict, description="출처ID -> 'unit' | 'period' (비교하지 않은 사유)"
    )
    quotes: List[Quote] = Field(default_factory=list)
    uncertainties: List[Uncertainty] = Field(default_factory=list)
    origin: Literal["llm", "extracted", "manual"] = "extracted"

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("text must not be blank")
        return v


class PlanItem(BaseModel):
    """Planner 가 만든 하위 조사 항목."""

    sub_question: str
    provider: ProviderName
    rationale: str
    keywords: List[str] = Field(default_factory=list)


class ConnectorError(BaseModel):
    """커넥터 실패 기록 (사용자에게 투명하게 노출)."""

    provider: ProviderName
    code: str
    message: str
    retryable: bool = False
    detail: Optional[str] = None


class LlmUsage(BaseModel):
    used: bool = False
    model: Optional[str] = None
    reason: Optional[str] = None
    calls: int = 0
    failures: int = 0
    notes: List[str] = Field(default_factory=list)


class RunStats(BaseModel):
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_seconds: float = 0.0
    retries_used: int = 0
    http_calls: int = 0
    sources_collected: int = 0
    providers_attempted: List[str] = Field(default_factory=list)


class RunStatus(str, Enum):
    COMPLETED = "completed"                  # 근거 있는 주장을 생성함
    PARTIAL = "partial"                      # 일부 커넥터 실패/타임아웃
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # 근거 부족: 단정하지 않음
    FAILED = "failed"                        # 전체 실패


class Briefing(BaseModel):
    """최종 리서치 브리핑."""

    run_id: str
    question: str
    topic: str = ""
    period: Optional[str] = None
    status: RunStatus = RunStatus.INSUFFICIENT_EVIDENCE
    plan: List[PlanItem] = Field(default_factory=list)
    sources: List[SourceDoc] = Field(default_factory=list)
    claims: List[Claim] = Field(default_factory=list)
    uncertainties: List[Uncertainty] = Field(default_factory=list)
    errors: List[ConnectorError] = Field(default_factory=list)
    data_gaps: List[str] = Field(default_factory=list)
    report_markdown: str = ""
    llm: LlmUsage = Field(default_factory=LlmUsage)
    outlook: Optional[OutlookSummary] = None
    claim_outlooks: List[ClaimOutlook] = Field(default_factory=list)
    stats: RunStats
    disclaimer: str = (
        "본 결과는 공개 자료를 바탕으로 자동 생성한 조사 초안입니다. "
        "투자 권유·매매 권유가 아니며, 최종 판단은 사용자 검토가 필요합니다."
    )

    @property
    def live_source_count(self) -> int:
        return sum(1 for s in self.sources if s.provenance.is_real)

    @property
    def sample_source_count(self) -> int:
        return sum(1 for s in self.sources if not s.provenance.is_real)


class ResearchRequest(BaseModel):
    """사용자 입력."""

    question: str = Field(..., min_length=2, max_length=2000)
    topic: str = Field("", max_length=200)
    company: str = Field("", max_length=200)
    period: str = Field("", max_length=100)
    balance: Optional[float] = Field(None, ge=10_000)
    ir_urls: List[str] = Field(default_factory=list, max_length=10)
    extra_keywords: List[str] = Field(default_factory=list, max_length=20)
    allow_sample_data: bool = True

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) < 2:
            raise ValueError("question is too short")
        return v

    @field_validator("ir_urls")
    @classmethod
    def _validate_urls(cls, v: List[str]) -> List[str]:
        out = []
        for u in v:
            u = (u or "").strip()
            if not u:
                continue
            if not u.startswith("https://"):
                raise ValueError("IR URL must use https")
            out.append(u)
        return out


# ---------------------------------------------------------------------------
# 전망 / 시뮬레이션
# ---------------------------------------------------------------------------
class OutlookDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    NEUTRAL = "neutral"


class ClaimOutlook(BaseModel):
    """주장별 방향성 판단(근거 인용 필수)."""

    claim_id: str
    direction: OutlookDirection = OutlookDirection.NEUTRAL
    strength: int = Field(1, ge=1, le=3)
    rationale: str = ""
    source_ids: List[str] = Field(default_factory=list)
    method: Literal["llm", "deterministic"] = "deterministic"


class OutlookSummary(BaseModel):
    """상승/하락 확률과 기대수익률(방법론을 함께 공개한다)."""

    probability_up: float = Field(0.5, ge=0.0, le=1.0)
    probability_down: float = Field(0.5, ge=0.0, le=1.0)
    expected_return: float = 0.0
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    method: str = ""
    bull_evidence: List[str] = Field(default_factory=list)
    bear_evidence: List[str] = Field(default_factory=list)
    neutral_evidence: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    disclaimer: str = (
        "AI 추정치이며 투자 권유가 아닙니다. 근거 수치와 방법론을 함께 확인하십시오."
    )


class PricePoint(BaseModel):
    """주가 포인트."""

    date: str
    close: float
    volume: Optional[float] = None


class SimulationScenario(BaseModel):
    """시나리오 하나의 경로."""

    key: str
    label: str
    color: str
    points: List[PricePoint] = Field(default_factory=list)
    final_price: float = 0.0
    return_pct: float = 0.0
    probability: float = 0.0
    description: str = ""


class SimulationPoint(BaseModel):
    """차트 위 한 점. hover 시 전망을 보여준다."""

    date: str
    price: float
    cumulative_return: float = 0.0
    outlook: str = ""
    detail: str = ""
    evidence: List[str] = Field(default_factory=list)
    probability: float = 0.0


class SimulationResult(BaseModel):
    """주식 시뮬레이션 결과."""

    symbol: Optional[str] = None
    company: Optional[str] = None
    currency: str = "KRW"
    current_price: Optional[float] = None
    history: List[PricePoint] = Field(default_factory=list)
    scenarios: List[SimulationScenario] = Field(default_factory=list)
    outlook_points: List[SimulationPoint] = Field(default_factory=list)
    outlook: Optional[OutlookSummary] = None
    balance: Optional[float] = None
    generated_at: datetime
    data_source: str = ""
    notes: List[str] = Field(default_factory=list)
    disclaimer: str = (
        "본 시뮬레이션은 공개 과거 주가와 AI 근거 판단을 결합한 교육용 예시입니다. "
        "실제 투자 수익을 예측하거나 보장하지 않습니다."
    )


# ---------------------------------------------------------------------------
# 대화형 온보딩 세션
# ---------------------------------------------------------------------------
class ChatStep(str, Enum):
    WELCOME = "welcome"      # "무엇이 궁금하신가요?"
    QUESTION = "question"    # 질문 입력 대기
    BALANCE = "balance"      # 잔고 입력 대기
    RUNNING = "running"      # 조사 중
    DONE = "done"            # 결과 완료


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: Optional[Dict[str, Any]] = None


class ChatReply(BaseModel):
    session_id: str
    step: ChatStep
    reply: str
    quick_replies: List[str] = Field(default_factory=list)
    state: Dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str = Field("", max_length=4000)
    balance: Optional[float] = None
