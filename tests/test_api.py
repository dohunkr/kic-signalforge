"""API 엔드포인트 테스트."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import ProviderName  # noqa: E402
from tests.helpers import FakeConnector, make_source  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KIC_DATA_DIR", str(tmp_path / "data"))
    for var in ("NVIDIA_API_KEY", "DART_API_KEY", "ECOS_API_KEY", "KOSIS_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    from app.api import routes
    from app.config import load_settings, reset_settings
    from app.pipeline.orchestrator import Orchestrator
    from app.storage.sqlite_store import SessionStore

    reset_settings()
    settings = load_settings()
    store = SessionStore(settings.sqlite_path)
    routes.reset_services()
    routes._store = store
    routes._orchestrator = Orchestrator(settings, store=store)

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_health_reports_connector_status(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    conns = data["connectors"]
    assert set(conns) >= {"nim", "dart", "ecos", "kosis", "ir"}
    # 키가 없으면 샘플/사용불가로 표시된다.
    assert conns["nim"]["configured"] is False
    assert conns["ecos"]["mode"] == "sample"
    assert conns["dart"]["configured"] is False


def test_research_endpoint_returns_briefing(client):
    source = make_source(
        "ecos:base_rate",
        raw_text="한국은행 기준금리는 2025-12 기준 3.25 % 입니다.",
    )
    from app.api import routes

    routes._orchestrator._connectors = {
        ProviderName.ECOS: FakeConnector(ProviderName.ECOS, sources=[source]),
        ProviderName.KOSIS: FakeConnector(ProviderName.KOSIS, sources=[]),
    }
    resp = client.post(
        "/api/research",
        json={"question": "한국은행 기준금리와 반도체 시장 영향은?", "period": "2025"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["question"]
    assert "report_markdown" in data
    assert "sources" in data and "claims" in data
    assert data["stats"]["duration_seconds"] >= 0


def test_research_validates_question(client):
    resp = client.post("/api/research", json={"question": "x"})
    assert resp.status_code == 422


def test_research_rejects_http_ir_url(client):
    resp = client.post(
        "/api/research",
        json={"question": "테스트 질문입니다", "ir_urls": ["http://example.org/ir"]},
    )
    assert resp.status_code == 422


def test_runs_listing_and_markdown_download(client):
    source = make_source("ecos:base_rate", raw_text="한국은행 기준금리는 3.25 % 입니다.")
    from app.api import routes

    routes._orchestrator._connectors = {
        ProviderName.ECOS: FakeConnector(ProviderName.ECOS, sources=[source]),
        ProviderName.KOSIS: FakeConnector(ProviderName.KOSIS, sources=[]),
    }
    resp = client.post(
        "/api/research", json={"question": "한국은행 기준금리와 시장 영향은?"}
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    listing = client.get("/api/runs")
    assert listing.status_code == 200
    assert any(r["run_id"] == run_id for r in listing.json()["runs"])

    md = client.get(f"/api/runs/{run_id}/report.md")
    assert md.status_code == 200
    assert "# KIC SignalForge 리서치 브리핑" in md.text

    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_index_page_is_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "KIC SignalForge" in resp.text
