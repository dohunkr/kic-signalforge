"""실측으로 발견한 응답 형식/제약에 대한 회귀 테스트."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import EcosSettings, NimSettings  # noqa: E402
from app.connectors.ecos import EcosConnector  # noqa: E402
from app.connectors.kosis import KosisConnector, _coerce_json, _to_prd_format  # noqa: E402
from app.errors import ERR_MODEL_UNAVAILABLE, LlmError  # noqa: E402
from app.llm.nim import NimProvider  # noqa: E402
from app.models import Metric, ProviderName, Quote  # noqa: E402
from app.pipeline.claims import merge_llm_claims  # noqa: E402
from app.pipeline.parser import verify_quote  # noqa: E402
from tests.helpers import make_source  # noqa: E402


# --- NIM -----------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    """httpx.Client 을 대체한다."""

    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, **kwargs):
        self.calls.append(("GET", url))
        return self._responses.get(("GET", url), _FakeResponse(200, {"data": []}))

    def post(self, url, headers=None, json=None, **kwargs):
        self.calls.append(("POST", url))
        for key, value in self._responses.items():
            if key[0] == "POST":
                return value
        return _FakeResponse(404, {"detail": "not found"})


def _nim_settings() -> NimSettings:
    return NimSettings(
        api_key="dummy-key-value-123456",
        base_url="https://integrate.api.nvidia.com/v1",
        model="not-provisioned/model",
        timeout_seconds=5.0,
    )


def test_nim_skips_model_not_provisioned_for_account(monkeypatch):
    """목록에는 있어도 404 나는 모델은 건너뛰고 실제로 동작하는 모델을 고른다."""
    provider = NimProvider(_nim_settings(), max_retries=0)
    calls = []

    def fake_post(url, headers=None, json=None, **kwargs):
        calls.append(json["model"])
        if json["model"] == "not-provisioned/model":
            return _FakeResponse(404, {"detail": "Function 'x': Not found for account 'y'"})
        return _FakeResponse(200, {"choices": [{"message": {"content": '{"ok":1}'}}]})

    import httpx

    class C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def get(self, url, headers=None, **kw):
            return _FakeResponse(200, {"data": [{"id": "not-provisioned/model"}]})

        def post(self, url, headers=None, json=None, **kw):
            return fake_post(url, headers=headers, json=json, **kw)

    monkeypatch.setattr(httpx, "Client", C)
    result = provider.chat_json("s", "u", max_tokens=100)
    assert result == {"ok": 1}
    assert provider.model != "not-provisioned/model"
    assert any("사용 불가" in note for note in provider._model_notes)


def test_nim_raises_when_no_model_is_callable(monkeypatch):
    provider = NimProvider(_nim_settings(), max_retries=0)

    import httpx

    class C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def get(self, url, headers=None, **kw):
            return _FakeResponse(200, {"data": [{"id": "not-provisioned/model"}]})

        def post(self, url, headers=None, json=None, **kw):
            return _FakeResponse(404, {"detail": "not found for account"})

    monkeypatch.setattr(httpx, "Client", C)
    with pytest.raises(LlmError) as exc:
        provider.chat_json("s", "u", max_tokens=100)
    assert exc.value.code == ERR_MODEL_UNAVAILABLE


def test_nim_disables_thinking_and_requests_json(monkeypatch):
    """reasoning 모델이 토큰 예산을 다 쓰지 않도록 thinking 을 끄고 JSON 을 요청한다."""
    provider = NimProvider(_nim_settings(), max_retries=0)
    provider._resolved_model = "some/model"
    captured = {}

    import httpx

    class C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def post(self, url, headers=None, json=None, **kw):
            captured.update(json)
            return _FakeResponse(200, {"choices": [{"message": {"content": '{"a":1}'}}]})

    monkeypatch.setattr(httpx, "Client", C)
    provider.chat_json("s", "u", max_tokens=100)
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["response_format"] == {"type": "json_object"}


def test_nim_parses_plain_json_and_json_array():
    provider = NimProvider(_nim_settings())
    assert provider._parse_json('{"a": 1}') == {"a": 1}
    assert provider._parse_json('[{"text": "x"}]') == {"claims": [{"text": "x"}]}
    assert provider._parse_json('```json\n{"a": 2}\n```') == {"a": 2}


# --- ECOS ----------------------------------------------------------------
def test_ecos_row_cap_for_sample_key():
    assert EcosConnector(EcosSettings(api_key=None), http=None)._row_cap == 10
    assert EcosConnector(EcosSettings(api_key="k" * 40), http=None)._row_cap == 200


def test_ecos_uses_cycle_specific_date_format():
    connector = EcosConnector(EcosSettings(api_key=None), http=None)
    url = connector._statistic_url("722Y001", "M", "202501", "202512", "0101000")
    assert "20250101" not in url
    assert "/722Y001/M/202501/202512/0101000/" in url


# --- KOSIS ---------------------------------------------------------------
def test_kosis_response_is_top_level_array():
    """실측: KOSIS 검색 응답은 JSON 배열이다."""
    data = _coerce_json('[{"TBL_ID": "DT_1"}]')
    assert data["result"][0]["TBL_ID"] == "DT_1"


def test_kosis_error_response_shape():
    data = _coerce_json('{"err": "11", "errMsg": "인증KEY가 유효하지 않습니다."}')
    assert data["err"] == "11"


def test_kosis_period_format_per_prd_se():
    assert _to_prd_format("20250101", "M") == "202501"
    assert _to_prd_format("20250101", "Y") == "2025"
    assert _to_prd_format("2025", "M") == "202501"


def test_kosis_parses_actual_row_fields():
    """실측 행 필드: 값 DT / 시점 PRD_DE / 단위 UNIT_NM / 최종갱신일 LST_CHN_DE."""
    connector = KosisConnector.__new__(KosisConnector)
    rows = [
        {
            "DT": "95.9",
            "PRD_DE": "202608",
            "UNIT_NM": "2020년100",
            "ITM_NM": "총지수",
            "C1_NM": "광공업(제외)",
            "C1": "0",
            "LST_CHN_DE": "2026-09-22",
            "PRD_SE": "M",
        }
    ]
    table = {
        "org_id": "101",
        "tbl_id": "DT_1JH20201",
        "tbl_nm": "산업생산지수(경상가격)",
        "org_nm": "국가데이터처",
        "link_url": "http://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1JH20201",
    }
    source = connector._build_source(table, rows, "http://x", "M", "202501", "202610")
    assert source is not None
    assert source.published_at == date(2026, 9, 22)
    assert len(source.metrics) == 1
    metric = source.metrics[0]
    assert metric.value == 95.9
    assert metric.unit == "2020년100"
    assert metric.period == "2026-08"
    assert "광공업" in metric.label


def test_kosis_prefers_total_breakdown_for_same_period():
    connector = KosisConnector.__new__(KosisConnector)
    rows = [
        {"DT": "91", "PRD_DE": "202510", "ITM_NM": "현재경기판단CSI", "C1": "A0001", "C1_NM": "남자"},
        {"DT": "93", "PRD_DE": "202510", "ITM_NM": "현재경기판단CSI", "C1": "99988", "C1_NM": "전체"},
    ]
    table = {"org_id": "101", "tbl_id": "T", "tbl_nm": "소비자동향조사", "org_nm": "x"}
    source = connector._build_source(table, rows, "http://x", "M", "202501", "202610")
    assert source is not None
    assert len(source.metrics) == 1
    assert source.metrics[0].value == 93.0


# --- 인용 대조 ------------------------------------------------------------
def test_verify_quote_accepts_separator_variation():
    src = "한국은행 기준금리 | 202512 | 2.5 연%\n한국은행 기준금리 | 202511 | 2.5 연%"
    assert verify_quote("한국은행 기준금리 | 202512 | 2.5 연%\n한국은행 기준금리 | 202511 | 2.5 연%", src)
    # 줄바꿈 대신 구분자로 이어붙인 경우
    assert verify_quote("한국은행 기준금리 / 202512 / 2.5 연% / 한국은행 기준금리 / 202511 / 2.5 연%", src)


def test_verify_quote_rejects_altered_content():
    src = "한국은행 기준금리 | 202512 | 2.5 연%"
    assert not verify_quote("한국은행 기준금리 | 202512 | 3.5 연%", src)
    assert not verify_quote("한국은행 기준금리는 2.5%다.", src)


# --- LLM 주장 병합(모델별 응답 형태 대응) ---------------------------------
def test_merge_llm_claims_accepts_flat_shape():
    text = "기준금리는 2.5%다."
    source = make_source("s1", raw_text=f"한국은행 기준금리 | 202512 | 2.5 연%\n{text}")
    out = merge_llm_claims(
        [
            {
                "text": text,
                "source_id": "s1",
                "quote": text,
                "metrics": [
                    {"label": "한국은행 기준금리", "value": 2.5, "raw_value": "2.5", "unit": "%"}
                ],
            }
        ],
        [source],
    )
    assert len(out) == 1
    assert out[0].quotes[0].verified


def test_merge_llm_claims_accepts_nested_evidence_shape():
    text = "기준금리는 2.5%다."
    source = make_source("s1", raw_text=text)
    out = merge_llm_claims(
        [{"claim": "기준금리 2.5%", "evidence": [{"source_id": "s1", "quote": text}]}],
        [source],
    )
    assert len(out) == 1
    assert out[0].supporting_source_ids == ["s1"]


def test_merge_llm_claims_drops_unverifiable_quote():
    source = make_source("s1", raw_text="실제 원문 내용")
    out = merge_llm_claims(
        [{"text": "없는 말", "source_id": "s1", "quote": "원문에 없는 인용"}],
        [source],
    )
    assert out == []


def test_merge_llm_claims_drops_unknown_source():
    source = make_source("s1", raw_text="원문")
    out = merge_llm_claims(
        [{"text": "주장", "source_id": "unknown", "quote": "원문"}],
        [source],
    )
    assert out == []
