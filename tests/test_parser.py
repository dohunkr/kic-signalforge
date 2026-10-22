"""파서 단위 테스트."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline.parser import (  # noqa: E402
    extract_metrics_from_text,
    extract_period,
    extract_published_at,
    html_to_text,
    labels_match,
    normalize_whitespace,
    parse_number,
    split_sentences,
    verify_quote,
)


def test_normalize_whitespace():
    assert normalize_whitespace("  a \n\t b  ") == "a b"


def test_html_to_text_strips_scripts_and_tags():
    html = """
    <html><head><style>.x{color:red}</style><script>var a=1;</script></head>
    <body><h1>제목</h1><p>본문 <b>내용</b>입니다.</p></body></html>
    """
    text = html_to_text(html)
    assert "var a=1" not in text
    assert "color:red" not in text
    assert "제목" in text and "본문" in text


def test_split_sentences():
    assert len(split_sentences("하나. 둘! 셋?")) == 3
    assert split_sentences("") == []


def test_verify_quote_requires_source_substring():
    src = "한국은행 기준금리는 3.25 % 입니다."
    assert verify_quote("한국은행 기준금리는 3.25 % 입니다.", src)
    assert verify_quote("한국은행 기준금리는 3.50 % 입니다.", src) is False
    # 공백 차이는 허용
    assert verify_quote("한국은행 기준금리는  3.25 % 입니다.", src)


def test_extract_period_variants():
    assert extract_period("2025년 3분기 매출") == "2025Q3"
    assert extract_period("2025-06 기준") == "2025-06"
    assert extract_period("2025년") == "2025"
    assert extract_period("기간 정보 없음") is None


def test_extract_published_at():
    assert extract_published_at("게시일 2025-03-14 기준").isoformat() == "2025-03-14"
    assert extract_published_at("2025. 03. 14 발행").isoformat() == "2025-03-14"
    assert extract_published_at("날짜 없음") is None


def test_parse_number_handles_comma_and_minus():
    assert parse_number("1,234.5") == 1234.5
    assert parse_number("-3") == -3.0
    assert parse_number("−3") == -3.0
    assert parse_number("abc") is None


def test_labels_match_exact_similar_and_different():
    assert labels_match("한국은행 기준금리", "한국은행 기준금리")
    # 포함 관계는 같은 지표로 본다
    assert labels_match("영업이익", "영업이익(십억원)")
    # 서로 다른 회계 개념은 구분한다
    assert not labels_match("영업이익", "당기순이익")
    assert not labels_match("영업이익", "원/달러 환율")


def test_extract_metrics_from_text():
    text = "2025년 한국은행 기준금리는 3.25 % 로 유지되었습니다."
    metrics = extract_metrics_from_text(text, source_id="s1")
    assert metrics
    metric = metrics[0]
    assert metric.value == 3.25
    assert metric.unit == "%"
    assert metric.source_id == "s1"
    assert "3.25" in text


def test_extract_metrics_requires_unit():
    text = "2025년 매출은 1,234 입니다."
    metrics = extract_metrics_from_text(text)
    # 단위가 붙지 않은 숫자는 라벨 유추가 어려워 채택하지 않는다
    for metric in metrics:
        assert metric.unit


def test_derive_label_korean_topic_particle():
    metrics = extract_metrics_from_text("한국은행 기준금리는 2025-12 기준 3.25 % 입니다.")
    assert metrics[0].label == "한국은행 기준금리"
    assert metrics[0].value == 3.25
    assert metrics[0].unit == "%"
    assert metrics[0].period == "2025-12"


def test_derive_label_keeps_leading_year_with_subject():
    metrics = extract_metrics_from_text("2025년 매출은 1,234억원 입니다.")
    assert metrics[0].label == "2025년 매출"
    assert metrics[0].value == 1234.0


def test_derive_label_english():
    metrics = extract_metrics_from_text(
        "The Korean economy recorded strong GDP growth of 3.8% in 2026."
    )
    assert metrics[0].value == 3.8
    assert "3.8" not in metrics[0].label


def test_derive_label_rejects_value_only_fragment():
    assert extract_metrics_from_text("2025년 3.25 % 증가했습니다.") == []


def test_raw_value_does_not_duplicate_unit():
    metrics = extract_metrics_from_text("영업이익은 12.3% 증가했다.")
    assert metrics[0].raw_value == "12.3"
    assert metrics[0].unit == "%"
