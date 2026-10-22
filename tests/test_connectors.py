"""커넥터 단위 테스트 (네트워크 호출 없음)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.connectors.base import ConnectorRequest  # noqa: E402
from app.connectors.dart import DartConnector  # noqa: E402
from app.connectors.ecos import EcosConnector, ECOS_INDICATORS  # noqa: E402
from app.connectors.ir import IrConnector  # noqa: E402
from app.connectors.kosis import KosisConnector  # noqa: E402
from app.config import DartSettings, EcosSettings, KosisSettings  # noqa: E402
from app.errors import (  # noqa: E402
    ERR_AUTH,
    ERR_QUOTA,
    map_dart_status,
    map_ecos_code,
    map_http_status,
    map_kosis_err,
)
from app.models import ProviderName  # noqa: E402


# --- 오류 코드 매핑 -------------------------------------------------------
def test_dart_status_mapping():
    assert map_dart_status("000")[0] is None
    assert map_dart_status("010")[0] == ERR_AUTH
    assert map_dart_status("013")[0] == "not_found"
    assert map_dart_status("020")[0] == ERR_QUOTA
    assert map_dart_status("021")[0] == ERR_QUOTA
    assert map_dart_status("901")[0] == ERR_AUTH


def test_ecos_code_mapping():
    assert map_ecos_code("ERROR-100")[0] == "invalid_request"
    assert map_ecos_code("ERROR-101")[0] == "not_found"
    assert map_ecos_code("ERROR-103")[0] == ERR_AUTH
    assert map_ecos_code("ERROR-104")[0] == ERR_QUOTA


def test_kosis_err_mapping():
    assert map_kosis_err("10")[0] == ERR_AUTH
    assert map_kosis_err("11")[0] == ERR_AUTH
    assert map_kosis_err("30")[0] == "not_found"
    assert map_kosis_err("40")[0] == ERR_QUOTA
    assert map_kosis_err("41")[0] == ERR_QUOTA


def test_nim_http_status_mapping():
    assert map_http_status(401, "nim")[0] == ERR_AUTH
    assert map_http_status(410, "nim")[0] == "model_unavailable"
    assert map_http_status(429, "nim")[0] == ERR_QUOTA
    assert map_http_status(503, "nim")[0] == "server_error"


# --- 커넥터 동작 ----------------------------------------------------------
def test_dart_without_key_reports_auth_error():
    connector = DartConnector(DartSettings(api_key=None), http=None)
    result = connector.fetch(ConnectorRequest(query="테스트", company="삼성전자"))
    assert result.errors
    assert result.errors[0].code == ERR_AUTH
    assert result.sources == []
    assert connector.mode == "unavailable"


def test_kosis_without_key_reports_auth_error():
    connector = KosisConnector(KosisSettings(api_key=None), http=None)
    result = connector.fetch(ConnectorRequest(query="인구"))
    assert result.errors
    assert result.errors[0].code == ERR_AUTH
    assert connector.mode == "unavailable"


def test_ecos_uses_sample_key_when_unconfigured():
    settings = EcosSettings(api_key=None)
    assert settings.effective_key == "sample"
    assert settings.using_sample_key is True
    connector = EcosConnector(settings, http=None)
    assert connector.mode == "sample"


def test_ecos_indicator_registry_codes_are_consistent():
    for indicator in ECOS_INDICATORS:
        assert indicator["stat_code"]
        assert indicator["item_code"]
        assert indicator["cycle"] in {"A", "M", "D", "Q"}
        assert indicator["label"]
        assert indicator["keywords"]


def test_ecos_url_contains_key_segment():
    connector = EcosConnector(EcosSettings(api_key="SECRETKEY"), http=None)
    url = connector._statistic_url("722Y001", "M", "202501", "202512", "0101000")
    assert url.startswith("https://ecos.bok.or.kr/api/StatisticSearch/SECRETKEY/json/kr/")
    assert "722Y001/M/202501/202512/0101000" in url


def test_ir_rejects_non_https():
    from app.errors import ConnectorFailure

    connector = IrConnector(http=None)
    with pytest.raises(ConnectorFailure) as exc:
        connector._fetch_one("http://example.org/ir")
    assert exc.value.code == "invalid_request"


def test_ir_fetch_without_url_is_noop():
    connector = IrConnector(http=None)
    result = connector.fetch(ConnectorRequest(query="테스트"))
    assert result.sources == []
    assert result.notes


def test_ecos_period_formatting():
    from datetime import date

    assert EcosConnector._format_date(date(2025, 3, 15), "M") == "202503"
    assert EcosConnector._format_date(date(2025, 3, 15), "D") == "20250315"
    assert EcosConnector._format_date(date(2025, 3, 15), "A") == "2025"
    assert EcosConnector._format_date(date(2025, 3, 15), "Q") == "2025Q1"


def test_ecos_period_from_time():
    assert EcosConnector._period_from_time("20251019", "D") == "2025-10-19"
    assert EcosConnector._period_from_time("202510", "M") == "2025-10"
    assert EcosConnector._period_from_time("2025", "A") == "2025"
