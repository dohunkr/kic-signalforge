"""Planner - 질문 분해 및 데이터 소스 선택.

LLM 이 없어도 동작해야 하므로 규칙 기반으로 구현한다.
LLM 이 있으면(선택적으로) LLM 이 만든 하위 항목을 함께 사용한다.
"""

from __future__ import annotations

import re
from typing import List, Optional

from ..models import PlanItem, ProviderName, ResearchRequest

#: 질문을 쪼갤 구분자
_SPLIT_RE = re.compile(
    r"\s*(?:그리고|또한|그러나|한편|그리고|무엇과|와\s*참고|과\s*비교|와\s*비교|및|그리고|,\s*|\?|？)\s*"
)

#: 거시 지표 키워드
_MACRO_HINTS = [
    "금리",
    "환율",
    "인플레이션",
    "물가",
    "소비자",
    "gdp",
    "경제",
    "통화",
    "미국",
    "연준",
    "생산지수",
    "산업생산",
    "국민총생산",
    "실질",
    "interest",
    "rate",
    "exchange",
    "inflation",
]

#: 산업/국가 통계 키워드
_NATIONAL_HINTS = [
    "산업",
    "시장",
    "점유율",
    "수출",
    "수입",
    "생산",
    "고용",
    "임금",
    "반도체",
    "배터리",
    "자동차",
    "조선",
    "철강",
    "화학",
    "유통",
    "소비",
    "industry",
    "market",
    "export",
    "employment",
]

#: 기업 공시가 유효한 것으로 보는 질문 패턴
_COMPANY_HINTS = ["실적", "재무", "매출", "영업이익", "공시", "사업보고서", "감사", "원장", "earnings", "revenue"]

#: 특정 분기/연도 표기
_PERIOD_RE = re.compile(r"(?:19|20)\d{2}")


def _split_question(question: str) -> List[str]:
    parts = [p.strip(" ,.·-—") for p in _SPLIT_RE.split(question)]
    out: List[str] = []
    seen = set()
    for p in parts:
        if len(p) < 2:
            continue
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _detect_company(request: ResearchRequest) -> Optional[str]:
    if request.company.strip():
        return request.company.strip()
    for text in (request.question, request.topic):
        m = re.search(r"([가-힣A-Za-z0-9&]{2,20}?)\s*(?:의|는|가|의)\s*(?:실적|재무|주주|공시|사업)", text)
        if m:
            return m.group(1).strip()
    return None


def plan(request: ResearchRequest) -> List[PlanItem]:
    """하위 조사 항목과 사용할 데이터 소스를 결정한다."""
    items: List[PlanItem] = []
    haystack = " ".join(
        [request.question, request.topic, request.company, " ".join(request.extra_keywords)]
    ).lower()

    sub_questions = _split_question(request.question)
    company = _detect_company(request)

    # 1) 질문 자체를 가장 먼저 조사한다.
    items.append(
        PlanItem(
            sub_question=request.question.strip(),
            provider=ProviderName.ECOS if _has_any(haystack, _MACRO_HINTS) else ProviderName.IR,
            rationale="사용자의 원문 질문을 그대로 1차 조사 대상으로 삼습니다.",
            keywords=list(request.extra_keywords),
        )
    )

    # 2) 분해된 하위 질문
    for sq in sub_questions[1:6]:
        provider = ProviderName.ECOS
        if _has_any(sq.lower(), _NATIONAL_HINTS):
            provider = ProviderName.KOSIS
        elif _has_any(sq.lower(), _COMPANY_HINTS) and company:
            provider = ProviderName.DART
        items.append(
            PlanItem(
                sub_question=sq,
                provider=provider,
                rationale="질문을 접속·병렬 표현으로 분리한 하위 항목입니다.",
                keywords=[sq],
            )
        )

    # 3) 데이터 소스 보강
    # 기업 전망 질문에 '반도체/산업/시장' 같은 단어가 들어갔다는 이유만으로
    # KOSIS의 무관한 통계를 붙이지 않는다. 기업이 특정되면 공시를 우선한다.
    if company:
        items.append(
            PlanItem(
                sub_question=f"{company}의 공시·재무 자료 확인",
                provider=ProviderName.DART,
                rationale="기업 관련 질문이므로 금융감독원 OpenDART 공시를 확인합니다.",
                keywords=["실적", "공시", "재무"],
            )
        )
    if _has_any(haystack, _MACRO_HINTS):
        items.append(
            PlanItem(
                sub_question="관련 거시지표(금리·환율·소비자신뢰) 확인",
                provider=ProviderName.ECOS,
                rationale="질문에 거시 지표 키워드가 있어 한국은행 ECOS 통계를 확인합니다.",
                keywords=["금리", "환율", "소비"],
            )
        )
    explicit_national_data = _has_any(
        haystack,
        ["국가 통계", "국가데이터", "산업생산", "수출 통계", "수입 통계", "산업 통계"],
    )
    if _has_any(haystack, _NATIONAL_HINTS) and (company is None or explicit_national_data):
        items.append(
            PlanItem(
                sub_question="관련 산업·국가 통계 확인",
                provider=ProviderName.KOSIS,
                rationale="질문에 산업/시장 키워드가 있어 국가데이터처 KOSIS 통계를 확인합니다.",
                keywords=["산업", "시장", "생산"],
            )
        )
    if request.ir_urls:
        items.append(
            PlanItem(
                sub_question="사용자가 제공한 기업 공식 IR 자료 확인",
                provider=ProviderName.IR,
                rationale="사용자가 직접 지정한 공식 IR URL 입니다.",
                keywords=[],
            )
        )

    # 중복 제거
    deduped: List[PlanItem] = []
    seen = set()
    for item in items:
        key = (item.provider.value, item.sub_question[:60])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _has_any(haystack: str, hints: List[str]) -> bool:
    return any(h in haystack for h in hints)
