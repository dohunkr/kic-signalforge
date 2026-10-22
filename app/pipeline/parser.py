"""파서 - 원문에서 메타데이터와 수치를 추출한다.

원칙
  * 인용문은 반드시 원문 정규화 부분 문자열로 검증한다(hallucination 방지).
  * 수치는 원문에 실제로 등장한 표기만 채택한다.
"""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..models import Metric, SourceDoc

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+|\n{2,}")
_LINE_SPLIT_RE = re.compile(r"\n+")

#: 한국어/영문 숫자 표기. 단위 후보도 함께 포착한다.
_NUMBER_RE = re.compile(
    r"(?P<sign>[-−+]?)"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>%|퍼센트|억원|조원|만원|원|달러|달러당|배|포인트|pct|pp|bps|명|천명|백만|억|조)?"
)

#: 문서에서 기준 기간을 추론하기 위한 패턴 (우선순위 순)
_PERIOD_PATTERNS = [
    (re.compile(r"(?P<y>20\d{2})\s*년\s*(?P<q>[1-4])\s*분기"), lambda m: f"{m['y']}Q{m['q']}"),
    (
        re.compile(
            r"(?P<y>20\d{2})\s*[-./]\s*(?P<m>0?[1-9]|1[0-2])\s*[-./]\s*(?P<d>0?[1-9]|[12]\d|3[01])(?!\d)"
        ),
        lambda m: f"{m['y']}-{int(m['m']):02d}-{int(m['d']):02d}",
    ),
    (
        re.compile(r"(?P<y>20\d{2})\s*년\s*(?P<m>0?[1-9]|1[0-2])\s*월"),
        lambda m: f"{m['y']}-{int(m['m']):02d}",
    ),
    (
        re.compile(r"(?P<y>20\d{2})\s*[-./]\s*(?P<m>0?[1-9]|1[0-2])(?![-/.\d])"),
        lambda m: f"{m['y']}-{int(m['m']):02d}",
    ),
    (re.compile(r"(?P<y>20\d{2})\s*Q(?P<q>[1-4])\b", re.I), lambda m: f"{m['y']}Q{m['q']}"),
    (re.compile(r"(?P<y>20\d{2})\s*년"), lambda m: str(m["y"])),
]

_DATE_PATTERNS = [
    re.compile(r"(20\d{2})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})"),
    re.compile(r"(20\d{2})[-./년]\s*(\d{1,2})월?$"),
]

#: 문장 앞에서 지표명 후보를 잡기 위한 접두 표현
_LABEL_LEAD = re.compile(r"^(?P<label>[가-힣A-Za-z0-9()（）·\s]{2,30}?)\s*(?:는|은|이|가)?\s*[:：]?\s*$")

_STOPWORDS = {
    "그리고",
    "그러나",
    "또한",
    "한편",
    "따라서",
    "한편으로",
    "기타",
    "등",
    "및",
    "또는",
}


def normalize_whitespace(text: str) -> str:
    """공백/줄바꿈을 정규화한다(인용 대조용)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u00a0", " ")
    return _WS_RE.sub(" ", text).strip()


def html_to_text(raw_html: str) -> str:
    """HTML을 읽을 수 있는 평문으로 변환한다."""
    if not raw_html:
        return ""
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = soup.get_text("\n")
    except Exception:  # noqa: BLE001 - bs4 미설치/파싱 실패 시 정규식 폴백
        text = _TAG_RE.sub(" ", raw_html)
        text = html.unescape(text)
    lines = [ln.strip() for ln in _LINE_SPLIT_RE.split(text)]
    return "\n".join(ln for ln in lines if ln)


def split_sentences(text: str) -> List[str]:
    """문장 단위 분리."""
    if not text:
        return []
    normalized = "\n".join(
        ln.strip() for ln in _LINE_SPLIT_RE.split(text) if ln.strip()
    )
    out: List[str] = []
    for chunk in _SENT_SPLIT_RE.split(normalized):
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out


def verify_quote(quote: str, source_text: str) -> bool:
    """인용문이 원문에 실제로 존재하는지 검증한다.

    1) 공백 정규화 후 부분 문자열
    2) 구분자(| / ·)까지 정규화한 부분 문자열 (표 형식 원문 대응)
    짧은 문자열은 우연한 일치를 막기 위해 최소 길이를 요구한다.
    """
    q = normalize_whitespace(quote)
    src = normalize_whitespace(source_text)
    if not q or not src:
        return False
    if q in src:
        return True
    # 공백만 다른 경우
    compact_q = q.replace(" ", "")
    compact_s = src.replace(" ", "")
    if len(compact_q) >= 8 and compact_q in compact_s:
        return True
    # 구분자를 다르게 표기한 경우(표 구조 원문)
    if len(q) >= 8:
        sep_q = _normalize_separators(q)
        sep_s = _normalize_separators(src)
        if sep_q in sep_s:
            return True
    return False


def _normalize_separators(text: str) -> str:
    """구분자(| / · ,)를 공백 하나로 통일한다."""
    return re.sub(r"\s*[|/·,]\s*", " ", text)


def find_excerpt(source_text: str, needle: str, width: int = 220) -> str:
    """needle 주변의 원문 구절을 잘라낸다."""
    if not source_text:
        return ""
    if not needle:
        return normalize_whitespace(source_text[:width])
    norm_src = normalize_whitespace(source_text)
    idx = norm_src.find(normalize_whitespace(needle))
    if idx == -1:
        compact = needle.replace(" ", "")
        idx = norm_src.replace(" ", "").find(compact)
        if idx == -1:
            return norm_src[:width]
        # 공백 제거 인덱스를 원본 인덱스로 근사 환산
        ratio = idx / max(len(compact), 1)
        idx = int(ratio * len(norm_src))
    start = max(0, idx - width // 3)
    end = min(len(norm_src), start + width)
    return norm_src[start:end]


def extract_period(text: str) -> Optional[str]:
    """문서/문장에서 기준 기간 표기를 추출한다."""
    if not text:
        return None
    head = text[:600]
    for pattern, fmt in _PERIOD_PATTERNS:
        m = pattern.search(head)
        if m:
            try:
                return fmt(m)
            except Exception:  # noqa: BLE001
                continue
    return None


def extract_published_at(text: str, fallback: Optional[str] = None) -> Optional[date]:
    """문서에서 발행일을 추출한다."""
    if text:
        for pattern in _DATE_PATTERNS:
            m = pattern.search(text[:4000])
            if m:
                try:
                    if m.re.groups >= 3:
                        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    return date(int(m.group(1)), int(m.group(2)), 1)
                except ValueError:
                    continue
    if fallback:
        try:
            return datetime.fromisoformat(fallback).date()
        except ValueError:
            return None
    return None


def normalize_label(label: str) -> str:
    """지표명 비교용 정규화 키."""
    if not label:
        return ""
    label = unicodedata.normalize("NFC", label).lower()
    label = re.sub(r"\s+", " ", label).strip()
    label = re.sub(r"[^\w가-힣 %]", "", label)
    return label.replace(" ", "")


def label_tokens(label: str) -> List[str]:
    """지표명 토큰 목록."""
    if not label:
        return []
    cleaned = re.sub(r"[^\w가-힣\s]", " ", unicodedata.normalize("NFC", label))
    return [t for t in cleaned.split() if t and t not in _STOPWORDS and len(t) > 1]


def labels_match(a: str, b: str, *, threshold: float = 0.6) -> bool:
    """두 지표명이 같은 지표를 가리키는지.

    1) 정규화 후 완전 일치
    2) 한쪽이 다른 쪽을 포함 (예: "영업이익" ⊂ "영업이익(십억원)")
    3) 토큰 Jaccard 유사도
    """
    na, nb = normalize_label(a), normalize_label(b)
    if na and na == nb:
        return True
    if na and nb:
        short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
        if len(short) >= 3 and short in long:
            return True
    ta, tb = set(label_tokens(a)), set(label_tokens(b))
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    union = len(ta | tb)
    return union > 0 and (inter / union) >= threshold


def parse_number(raw: str) -> Optional[float]:
    """'1,234.5' 같은 표기를 float 로 변환한다."""
    if raw is None:
        return None
    cleaned = str(raw).strip().replace(",", "").replace("−", "-")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_metrics_from_text(
    text: str, *, source_id: Optional[str] = None, limit: int = 40
) -> List[Metric]:
    """본문 문장에서 수치+단위를 추출한다(서사형 자료용).

    라벨은 문장 앞부분에서 "…은/는 …" 형태의 주어를 최대한 잘라 낸다.
    """
    metrics: List[Metric] = []
    seen: set = set()
    for sentence in split_sentences(text):
        if len(metrics) >= limit:
            break
        for m in _NUMBER_RE.finditer(sentence):
            num = m.group("number")
            unit = (m.group("unit") or "").strip()
            if not unit:
                continue
            value = parse_number(m.group("sign") + num)
            if value is None:
                continue
            label = _derive_label(sentence, m.start())
            if not label:
                continue
            key = (label, value, unit)
            if key in seen:
                continue
            seen.add(key)
            metrics.append(
                Metric(
                    label=label,
                    value=value,
                    raw_value=(m.group("sign") or "") + num,
                    unit=unit,
                    period=extract_period(sentence),
                    source_id=source_id,
                    context=sentence[:400],
                )
            )
            break  # 문장당 1개 수치만 채택(노이즈 감소)
    return metrics


#: 라벨 뒤에 붙는 영어 기능어(끝에서부터 제거)
_EN_TRAILING_STOPWORDS = {
    "by", "from", "and", "to", "of", "the", "in", "on", "at", "for", "with",
    "a", "an", "is", "are", "was", "were", "that", "this", "than", "but",
    "also", "while", "when", "after", "before", "since", "until", "has",
    "have", "had", "been", "as", "its", "their", "our", "up", "down",
}

#: 지표명 끝에 숫자값이 붙어 있으면 라벨이 아니라 본문 조각이다
_VALUE_TOKEN_AT_END = re.compile(r"\d+(?:[.,]\d+)?\s*[%원배]?\s*$")

#: 한국어 조사(은/는/이/가)로 주어를 구분한다
_KO_PARTICLE_SUFFIX = ("은", "는", "이", "가")


def _derive_label(sentence: str, number_pos: int) -> str:
    """문장에서 수치 앞부분을 지표명 후보로 만든다.

    * 한국어: 마지막 주격조사(은/는/이/가) 앞의 명사구를 지표명으로 삼는다.
    * 영어: 뒤에 붙은 기능어를 제거하고 최대 6단어만 남긴다.
    * 줄바꿈 뒤는 문맥이 끊기므로 같은 줄만 본다.
    """
    prefix = sentence[:number_pos]
    if "\n" in prefix:
        prefix = prefix.rsplit("\n", 1)[-1]
    prefix = prefix.strip()
    if not prefix:
        return ""
    prefix = re.sub(r"^[\s\-–—·,.]+", "", prefix)
    words = prefix.split()
    if not words:
        return ""

    # 한국어: 마지막 주격조사 단어를 찾는다.
    particle_index = -1
    for index in range(len(words) - 1, -1, -1):
        if words[index].endswith(_KO_PARTICLE_SUFFIX):
            particle_index = index
            break

    if particle_index >= 0:
        head = words[particle_index]
        head = re.sub(r"(은|는|이|가)$", "", head)
        window = words[max(0, particle_index - 4) : particle_index]
        candidate = " ".join([*window, head]).strip()
    else:
        # 영어: 기능어를 걷어내고 뒤쪽 최대 6단어만 남긴다.
        trimmed = list(words)
        while trimmed and trimmed[-1].lower().strip(".,:;·") in _EN_TRAILING_STOPWORDS:
            trimmed.pop()
        while trimmed and trimmed[0].lower().strip(".,:;·") in _EN_TRAILING_STOPWORDS:
            trimmed.pop(0)
        candidate = " ".join(trimmed[-6:]) if trimmed else ""

    candidate = re.sub(r"\s*[-–—]\s*$", "", candidate).strip(" ,.:;·")
    candidate = candidate.strip(" ,.:;·-")
    candidate = re.sub(r"[\(\[\{]+$", "", candidate).strip()
    # "[헤드라인] 본문" 형태에서 괄호 앞 접두사는 제거한다.
    if "]" in candidate:
        tail = candidate.rsplit("]", 1)[1].strip(" -–—:")
        if len(tail) >= 3:
            candidate = tail
    if len(candidate) < 2 or len(candidate) > 60:
        return ""
    if candidate.isdigit():
        return ""
    # 연도/날짜만 남은 조각은 지표명이 아니다.
    if re.fullmatch(r"[\d\-\.\s년월일Qq/]+", candidate):
        return ""
    # 숫자값이 끝에 붙은 조각은 지표명이 아니다.
    if _VALUE_TOKEN_AT_END.search(candidate):
        return ""
    if candidate.count(" ") > 6:
        return ""
    return candidate


def source_lineage(source: SourceDoc) -> Dict[str, Any]:
    """출처 표시용 요약."""
    return {
        "source_id": source.source_id,
        "provider": source.provider.value,
        "title": source.title,
        "url": source.url,
        "published_at": source.published_at.isoformat() if source.published_at else None,
        "period_label": source.period_label,
        "unit": source.unit,
        "provenance": source.provenance.value,
        "provenance_label": source.provenance.label,
    }


def iter_source_refs(sources: Iterable[SourceDoc]) -> Dict[str, SourceDoc]:
    return {s.source_id: s for s in sources}
