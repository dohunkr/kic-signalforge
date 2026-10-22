"""API 접속 상태 점검 스크립트.

각 제공기관의 실제 접근 가능 여부와 오류 코드 매핑을 확인한다.
키가 없으면 "키 없음"으로 명시하고, 임의 데이터를 만들어내지 않는다.

    python scripts/probe_apis.py
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402

TIMEOUT = 30.0


def _get(url: str, *, params=None, headers=None, binary=False):
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return 0, b"" if binary else f"{type(exc).__name__}: {exc}", None
    if binary:
        return resp.status_code, resp.content, resp
    return resp.status_code, resp.text, resp


def _json(url: str, *, params=None, headers=None):
    status, body, resp = _get(url, params=params, headers=headers)
    try:
        return status, json.loads(body)
    except Exception:  # noqa: BLE001
        return status, body[:400]


def probe_nim() -> None:
    print("=" * 78)
    settings = get_settings()
    print(f"NVIDIA NIM  ({settings.nim.base_url})")
    headers = {"Authorization": f"Bearer {settings.nim.api_key}"} if settings.nim.api_key else None

    status, data = _json(f"{settings.nim.base_url.rstrip('/')}/models", headers=headers)
    ids = []
    if status == 200 and isinstance(data, dict):
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
    print(f"  GET /v1/models            -> HTTP {status} | {len(ids)}개 모델")
    if not settings.nim.api_key:
        print("     키 미설정: LLM 호출 불가")
        return
    print(f"     설정 모델: {settings.nim.model} (목록 포함: {settings.nim.model in ids})")

    # 실제 호출로 계정에 프로비저닝된 모델만 확인한다.
    ok_models = []
    for candidate in [settings.nim.model] + [
        "nvidia/nemotron-3-ultra-550b-a55b",
        "nvidia/nemotron-3.5-lightning-30b-a3b",
        "deepseek-ai/deepseek-v4.1-flash",
    ]:
        if candidate in ok_models:
            continue
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                resp = client.post(
                    f"{settings.nim.base_url.rstrip('/')}/chat/completions",
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "model": candidate,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 4,
                        "temperature": 0,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )
            elapsed = time.perf_counter() - started
            if resp.status_code == 200:
                ok_models.append(candidate)
                print(f"     사용 가능: {candidate} ({elapsed:.1f}s)")
            elif resp.status_code == 404:
                print(f"     계정 미제공: {candidate} (404)")
            else:
                print(f"     오류: {candidate} (HTTP {resp.status_code})")
        except Exception as exc:  # noqa: BLE001
            print(f"     오류: {candidate} ({type(exc).__name__})")
    if ok_models:
        print(f"     → 실제 호출 가능한 모델 {len(ok_models)}개 확인")


def probe_dart() -> None:
    print("=" * 78)
    settings = get_settings()
    print("금융감독원 OpenDART  (https://opendart.fss.or.kr/api)")
    if not settings.dart.api_key:
        print("  키 미설정: DART 수집 불가")
        return
    status, body, resp = _get(
        "https://opendart.fss.or.kr/api/corpCode.xml",
        params={"crtfc_key": settings.dart.api_key},
        binary=True,
    )
    print(f"  GET /corpCode.xml         -> HTTP {status} | {len(body) / 1e6:.1f} MB")
    status, data = _json(
        "https://opendart.fss.or.kr/api/list.json",
        params={
            "crtfc_key": settings.dart.api_key,
            "corp_code": "00126380",
            "bgn_de": "20260101",
            "end_de": "20261021",
        },
    )
    if isinstance(data, dict):
        print(
            f"  GET /list.json (삼성전자)  -> HTTP {status} | status={data.get('status')} "
            f"| {len(data.get('list') or [])}건"
        )
    else:
        print(f"  GET /list.json             -> HTTP {status} | {str(data)[:120]}")


def probe_ecos() -> None:
    print("=" * 78)
    settings = get_settings()
    print("한국은행 ECOS  (https://ecos.bok.or.kr/api)")
    key = settings.ecos.effective_key
    mode = "실제 키" if settings.ecos.configured else "공식 샘플 키('sample')"
    print(f"  키 모드: {mode}")
    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/10/"
        "722Y001/M/202510/202512/0101000/"
    )
    status, data = _json(url)
    if isinstance(data, dict) and "StatisticSearch" in data:
        rows = data["StatisticSearch"].get("row") or []
        latest = rows[-1] if rows else {}
        print(
            f"  GET /StatisticSearch      -> HTTP {status} | {len(rows)}건 "
            f"| 최신 {latest.get('TIME')} = {latest.get('DATA_VALUE')} {latest.get('UNIT_NAME')}"
        )
    else:
        print(f"  GET /StatisticSearch      -> HTTP {status} | {str(data)[:160]}")
    status, data = _json(
        f"https://ecos.bok.or.kr/api/KeyStatisticList/{key}/json/kr/1/10/"
    )
    if isinstance(data, dict) and "KeyStatisticList" in data:
        total = data["KeyStatisticList"].get("list_total_count")
        print(f"  GET /KeyStatisticList     -> HTTP {status} | 전체 {total}개 지표 중 10건")


def probe_kosis() -> None:
    print("=" * 78)
    settings = get_settings()
    print("국가데이터처 KOSIS  (https://kosis.kr/openapi)")
    if not settings.kosis.api_key:
        print("  키 미설정: KOSIS 수집 불가")
        return
    status, data = _json(
        "https://kosis.kr/openapi/statisticsSearch.do",
        params={
            "method": "getList",
            "apiKey": settings.kosis.api_key,
            "format": "json",
            "jsonVD": "Y",
            "searchNm": "산업생산",
            "resultCount": 3,
            "startCount": 1,
        },
    )
    if isinstance(data, list):
        print(f"  GET /statisticsSearch.do  -> HTTP {status} | {len(data)}개 통계표")
        for row in data[:2]:
            print(f"     {row.get('TBL_ID')} | {str(row.get('TBL_NM'))[:44]}")
        if data:
            first = data[0]
            status2, data2 = _json(
                "https://kosis.kr/openapi/Param/statisticsParameterData.do",
                params={
                    "method": "getList",
                    "apiKey": settings.kosis.api_key,
                    "format": "json",
                    "jsonVD": "Y",
                    "orgId": first["ORG_ID"],
                    "tblId": first["TBL_ID"],
                    "objL1": "ALL",
                    "itmId": "ALL",
                    "prdSe": "M",
                    "startPrdDe": "202501",
                    "endPrdDe": "202610",
                },
            )
            if isinstance(data2, list):
                rows = data2
                print(f"  GET /statisticsParameterData.do -> HTTP {status2} | {len(rows)}행")
                if rows:
                    last = rows[-1]
                    print(
                        f"     최신 {last.get('PRD_DE')} | {last.get('ITM_NM')} "
                        f"= {last.get('DT')} {last.get('UNIT_NM')}"
                    )
            else:
                print(f"  GET /statisticsParameterData.do -> HTTP {status2} | {str(data2)[:150]}")
    else:
        print(f"  GET /statisticsSearch.do  -> HTTP {status} | {str(data)[:180]}")


def main() -> None:
    print("KIC SignalForge - API 접속 상태 점검")
    print("=" * 78)
    probe_nim()
    probe_dart()
    probe_ecos()
    probe_kosis()
    print("=" * 78)
    print("키가 설정되지 않은 제공기관은 실제 데이터를 수집할 수 없습니다.")
    print("해당 기관은 '샘플/데모 모드'로 동작하며 결과에 그 사실이 표시됩니다.")


if __name__ == "__main__":
    main()
