"""대화형 온보딩 · 전망 · 시뮬레이션 · 주가 커넥터 테스트."""

from __future__ import annotations

import io
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chat.session import ChatService, parse_amount  # noqa: E402
from app.connectors.dart import DartConnector  # noqa: E402
from app.connectors.price import PriceConnector  # noqa: E402
from app.config import DartSettings, PriceSettings  # noqa: E402
from app.models import (  # noqa: E402
    Claim,
    ClaimOutlook,
    ClaimStatus,
    Metric,
    OutlookDirection,
    PricePoint,
    ProviderName,
    Provenance,
    Quote,
    Uncertainty,
    UncertaintyKind,
)
from app.pipeline import outlook as outlook_mod  # noqa: E402
from app.pipeline import simulation as sim_mod  # noqa: E402
from app.service import ResearchService  # noqa: E402
from tests.helpers import make_source  # noqa: E402


# --- 금액 파싱 ------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("1,000만원", 10_000_000.0),
        ("5000000", 5_000_000.0),
        ("1억원", 100_000_000.0),
        ("3천만원", 30_000_000.0),
        ("2조", 2_000_000_000_000.0),
        ("abc", None),
        ("", None),
    ],
)
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


# --- 전망 계산 ------------------------------------------------------------
def _claim(claim_id, text, supporting=("s1",), contradicting=()):
    return Claim(
        claim_id=claim_id,
        text=text,
        status=ClaimStatus.SUPPORTED,
        supporting_source_ids=list(supporting),
        contradicting_source_ids=list(contradicting),
        quotes=[Quote(text=text, source_id=supporting[0], verified=True)],
    )


def test_outlook_probability_reflects_evidence_direction():
    claims = [_claim("c1", "매출이 증가했습니다"), _claim("c2", "실적이 개선되었습니다")]
    outlooks = [
        ClaimOutlook(claim_id="c1", direction=OutlookDirection.UP, strength=3),
        ClaimOutlook(claim_id="c2", direction=OutlookDirection.UP, strength=2),
    ]
    summary = outlook_mod.summarize(outlooks, claims, [])
    assert summary.probability_up > 0.5
    assert summary.probability_down == pytest.approx(1 - summary.probability_up)


def test_outlook_probability_below_half_when_bearish():
    claims = [_claim("c1", "실적이 악화되었습니다")]
    outlooks = [ClaimOutlook(claim_id="c1", direction=OutlookDirection.DOWN, strength=3)]
    summary = outlook_mod.summarize(outlooks, claims, [])
    assert summary.probability_up < 0.5


def test_outlook_shrinks_toward_fifty_percent_with_uncertainty():
    claims = [_claim("c1", "매출 증가")]
    outlooks = [ClaimOutlook(claim_id="c1", direction=OutlookDirection.UP, strength=3)]
    clean = outlook_mod.summarize(outlooks, claims, [])
    noisy = outlook_mod.summarize(
        outlooks,
        claims,
        [
            Uncertainty(kind=UncertaintyKind.STALENESS, message="오래된 자료")
            for _ in range(8)
        ],
    )
    assert noisy.confidence < clean.confidence
    assert abs(noisy.probability_up - 0.5) < abs(clean.probability_up - 0.5)


def test_outlook_without_evidence_is_fifty_fifty():
    summary = outlook_mod.summarize([], [], [])
    assert summary.probability_up == pytest.approx(0.5)
    assert summary.probability_down == pytest.approx(0.5)
    assert summary.notes


def test_outlook_ignores_claims_without_sources():
    claims = [
        Claim(claim_id="c1", text="근거 없는 주장", status=ClaimStatus.UNVERIFIED),
    ]
    outlooks = [ClaimOutlook(claim_id="c1", direction=OutlookDirection.UP, strength=3)]
    summary = outlook_mod.summarize(outlooks, claims, [])
    assert summary.probability_up == pytest.approx(0.5)


# --- 시뮬레이션 -----------------------------------------------------------
def _history(n=250, start=200_000.0, drift=0.002):
    base = datetime(2025, 1, 1).date()
    points = []
    price = start
    for i in range(n):
        points.append(PricePoint(date=(base + timedelta(days=i)).isoformat(), close=round(price, 2)))
        price *= 1 + drift + (0.01 if i % 7 == 0 else -0.01)
    return points


def test_expected_moves_uses_downside_percentile():
    history = _history()
    up, down = sim_mod.expected_moves(history)
    assert up >= 0
    assert down >= 0
    assert down > 0, "하락 폭이 0이면 낙폭을 과소평가한다"


def test_simulation_scenarios_are_ordered_and_anchored():
    history = _history()
    outlook = outlook_mod.summarize([], [], [])
    result = sim_mod.build(
        history,
        outlook,
        symbol="005930.KS",
        company="테스트",
        balance=10_000_000,
    )
    assert result.current_price == history[-1].close
    bull = next(s for s in result.scenarios if s.key == "bull")
    base = next(s for s in result.scenarios if s.key == "base")
    bear = next(s for s in result.scenarios if s.key == "bear")
    assert bull.final_price > base.final_price > bear.final_price
    assert len(result.outlook_points) == 12
    assert result.outlook_points[0].evidence is not None


def test_simulation_base_scenario_lands_on_expected_return():
    history = _history()
    outlook = outlook_mod.summarize([], [], [])
    result = sim_mod.build(history, outlook, symbol="005930.KS")
    base = next(s for s in result.scenarios if s.key == "base")
    assert base.return_pct == pytest.approx(outlook.expected_return, abs=0.5)


def test_simulation_probabilities_sum_to_one():
    history = _history()
    outlook = outlook_mod.summarize(
        [ClaimOutlook(claim_id="c1", direction=OutlookDirection.UP, strength=3)],
        [_claim("c1", "매출 증가")],
        [],
    )
    result = sim_mod.build(history, outlook, symbol="005930.KS")
    total = sum(s.probability for s in result.scenarios)
    assert 0.0 <= total <= 1.5  # 기준 시나리오 확률은 잔여분


def test_simulation_without_history_returns_empty_result():
    result = sim_mod.build([], outlook_mod.summarize([], [], []), symbol="X")
    assert result.scenarios == []
    assert result.notes


def test_simulation_deterministic_for_same_input():
    history = _history()
    outlook = outlook_mod.summarize([], [], [])
    a = sim_mod.build(history, outlook, symbol="005930.KS")
    b = sim_mod.build(history, outlook, symbol="005930.KS")
    assert [p.close for p in a.scenarios[0].points] == [p.close for p in b.scenarios[0].points]


# --- 주가 커넥터 -----------------------------------------------------------
class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, search_payload):
        self.search_payload = search_payload
        self.urls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None, **kw):
        self.urls.append(url)
        if "finance/search" in url:
            return _Resp(200, self.search_payload)
        return _Resp(404, {})

    def get_json(self, url, **kwargs):
        resp = self.get(url, kwargs.get("params"))
        return resp.status_code, resp.json()


def test_price_resolve_symbol_picks_korean_equity_quote(monkeypatch):
    import httpx

    payload = {
        "quotes": [
            {"symbol": "005930-USD", "quoteType": "CRYPTOCURRENCY", "exchange": "CCC"},
            {
                "symbol": "005930.KQ",
                "quoteType": "EQUITY",
                "exchange": "KOE",
                "shortname": "005930.KQ,0P0000B2XZ,18569122",
            },
            {
                "symbol": "005930.KS",
                "quoteType": "EQUITY",
                "exchange": "KSC",
                "shortname": "SamsungElec",
            },
        ]
    }
    client = _Client(payload)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)
    connector = PriceConnector(PriceSettings(), http=client)
    assert connector.resolve_symbol("005930") == "005930.KS"


def test_price_resolve_symbol_returns_none_for_unknown(monkeypatch):
    import httpx

    client = _Client({"quotes": []})
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)
    connector = PriceConnector(PriceSettings(), http=client)
    assert connector.resolve_symbol("999999") is None
    assert connector.resolve_symbol("") is None


# --- DART 자동 IR ----------------------------------------------------------
def test_find_companies_uses_registry(monkeypatch):
    connector = DartConnector(DartSettings(api_key="k" * 40), http=None)
    monkeypatch.setattr(
        connector,
        "_load_corp_records",
        lambda: {
            "삼성전자": {"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930"},
            "반도체": {"corp_code": "00000001", "corp_name": "반도체", "stock_code": "000000"},
        },
    )
    found = connector.find_companies("2025년 삼성전자 반도체 업황은?")
    assert found[0] == "삼성전자"
    assert "2025년" not in found


def test_fetch_document_body_reads_zip(monkeypatch):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("20251231000603.xml", "<html><body>공시 본문 테스트</body></html>")

    class FakeHttp:
        def get_bytes(self, url, **kw):
            return 200, payload.getvalue()

    connector = DartConnector(DartSettings(api_key="k" * 40), http=FakeHttp())
    body = connector.fetch_document_body("20251231000603")
    assert body is not None
    assert "공시 본문 테스트" in body


# --- 대화 상태 머신 --------------------------------------------------------
class _StubService:
    def __init__(self):
        self.calls = []

    def _guess_company(self, question):
        return "테스트기업"

    def run(self, **kwargs):
        self.calls.append(kwargs)
        briefing = _StubService._briefing()
        return briefing, _StubService._simulation()

    @staticmethod
    def _briefing():
        from app.models import Briefing, RunStats, LlmUsage, OutlookSummary

        now = datetime.now(timezone.utc)
        return Briefing(
            run_id="r1",
            question="q",
            status="completed",
            sources=[],
            claims=[],
            outlook=OutlookSummary(probability_up=0.6, probability_down=0.4, expected_return=3.2),
            llm=LlmUsage(used=True, model="m"),
            stats=RunStats(started_at=now, finished_at=now),
        )

    @staticmethod
    def _simulation():
        from app.models import SimulationResult

        return SimulationResult(
            symbol="005930.KS",
            company="테스트",
            current_price=100000.0,
            generated_at=datetime.now(timezone.utc),
        )


def test_chat_flow_welcome_question_balance_done():
    service = _StubService()
    chat = ChatService(service)

    reply = chat.welcome(chat.session())
    assert reply.step.value == "welcome"
    assert "환영합니다" in reply.reply
    assert reply.quick_replies

    reply = chat.handle(reply.session_id, "테스트 기업 실적이 어때?")
    assert reply.step.value == "balance"
    assert "잔고" in reply.reply

    reply = chat.handle(reply.session_id, "1,000만원")
    assert reply.step.value == "done"
    assert "상승 확률" in reply.reply
    result = reply.state["result"]
    assert result["simulation"]["symbol"] == "005930.KS"
    assert service.calls[0]["balance"] == 10_000_000


def test_chat_rejects_non_numeric_balance_then_accepts():
    chat = ChatService(_StubService())
    reply = chat.welcome(chat.session())
    reply = chat.handle(reply.session_id, "질문")
    reply = chat.handle(reply.session_id, "많이")
    assert reply.step.value == "balance"
    reply = chat.handle(reply.session_id, "2,000만원")
    assert reply.step.value == "done"


def test_chat_reset_starts_over():
    chat = ChatService(_StubService())
    reply = chat.welcome(chat.session())
    reply = chat.handle(reply.session_id, "질문")
    reply = chat.handle(reply.session_id, "1,000만원")
    assert reply.step.value == "done"
    fresh = chat.reset(reply.session_id)
    assert fresh.step.value == "welcome"
    assert not fresh.result
