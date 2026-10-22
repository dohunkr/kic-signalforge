"""ECOS 커넥터의 샘플 키 제한/구간 분할 동작 테스트 (네트워크 없음)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.connectors.base import ConnectorRequest  # noqa: E402
from app.connectors.ecos import EcosConnector, ROW_CAP_SAMPLE  # noqa: E402
from app.config import EcosSettings  # noqa: E402


def _row(time: str, value: str, item2: str = None):
    return {
        "STAT_CODE": "722Y001",
        "STAT_NAME": "한국은행 기준금리",
        "ITEM_CODE1": "0101000",
        "ITEM_NAME1": "한국은행 기준금리",
        "ITEM_CODE2": item2,
        "ITEM_NAME2": "전체" if item2 else None,
        "UNIT_NAME": "연%",
        "TIME": time,
        "DATA_VALUE": value,
    }


def test_sample_key_uses_ten_row_cap():
    connector = EcosConnector(EcosSettings(api_key=None), http=None)
    assert connector._row_cap == ROW_CAP_SAMPLE == 10


def test_live_key_uses_larger_row_cap():
    connector = EcosConnector(EcosSettings(api_key="A" * 40), http=None)
    assert connector._row_cap > ROW_CAP_SAMPLE


def test_monthly_cycle_uses_yyyymm_format():
    connector = EcosConnector(EcosSettings(api_key=None), http=None)
    url = connector._statistic_url("722Y001", "M", "202501", "202512", "0101000")
    assert "/722Y001/M/202501/202512/0101000/" in url
    assert "20250101" not in url


def test_second_level_item_code_is_appended():
    connector = EcosConnector(EcosSettings(api_key=None), http=None)
    url = connector._statistic_url("511Y002", "M", "202501", "202512", "FMAB", "99988")
    assert url.endswith("/511Y002/M/202501/202512/FMAB/99988/")


def test_indicator_fetch_chunks_backwards_to_reach_latest_data():
    connector = EcosConnector(EcosSettings(api_key=None), http=None)
    calls: List[str] = []

    def fake_get_json(url: str, **kwargs: Any):
        calls.append(url)
        # 샘플 키는 1회 10건이므로 월 단위로 10개월 구간씩 나눠 호출한다.
        if url.endswith("/722Y001/M/202503/202512/0101000/"):
            return 200, {
                "StatisticSearch": {
                    "list_total_count": 10,
                    "row": [_row(f"2025{m:02d}", "2.5") for m in range(3, 13)],
                }
            }
        if url.endswith("/722Y001/M/202501/202502/0101000/"):
            return 200, {
                "StatisticSearch": {
                    "list_total_count": 2,
                    "row": [_row("202501", "2.75"), _row("202502", "2.75")],
                }
            }
        return 200, {"StatisticSearch": {"list_total_count": 0, "row": []}}

    connector._http = type("H", (), {"get_json": staticmethod(fake_get_json)})()

    request = ConnectorRequest(
        query="금리", start_date="20250101", end_date="20251231"
    )
    source = connector._fetch_indicator(
        {
            "key": "base_rate",
            "stat_code": "722Y001",
            "item_code": "0101000",
            "cycle": "M",
            "label": "한국은행 기준금리",
        },
        request,
    )
    assert source is not None
    # 최신 구간이 먼저 조회되어 최신 값이 대표로 노출되어야 한다.
    assert calls[0].endswith("/722Y001/M/202503/202512/0101000/")
    assert source.period_label == "2025-12"
    assert source.metrics[-1].value == 2.5
    assert len(source.metrics) == 12
    # 관측치는 시간 오름차순으로 정리된다.
    assert source.metrics[0].period == "2025-01"


def test_duplicate_timestamps_prefer_total_breakdown():
    connector = EcosConnector(EcosSettings(api_key=None), http=None)
    rows = [
        _row("202510", "91", "A0001"),   # 남자
        _row("202510", "93", "99988"),   # 전체
        _row("202510", "90", "A0002"),   # 여자
    ]

    def fake_get_json(url: str, **kwargs: Any):
        return 200, {"StatisticSearch": {"list_total_count": 3, "row": rows}}

    connector._http = type("H", (), {"get_json": staticmethod(fake_get_json)})()
    request = ConnectorRequest(query="소비", start_date="20250101", end_date="20251231")
    source = connector._fetch_indicator(
        {
            "key": "consumer_tendency",
            "stat_code": "511Y002",
            "item_code": "FMAB",
            "item_code2": "99988",
            "cycle": "M",
            "label": "소비자동향지수",
        },
        request,
    )
    assert source is not None
    assert source.metrics[0].value == 93.0
