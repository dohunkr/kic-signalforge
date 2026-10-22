"""데이터 소스 가용성 점검 (주가 / IR 자동수집 후보)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SignalForge/0.1"}


def probe(name: str, url: str, headers=None) -> None:
    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers=headers or UA) as c:
            r = c.get(url)
        head = r.text[:160].replace("\n", " ")
        print(f"{name:34s} HTTP {r.status_code} {len(r.content):>8}B ct={r.headers.get('content-type','')[:30]}")
        print(f"    {head}")
    except Exception as exc:  # noqa: BLE001
        print(f"{name:34s} ERR {type(exc).__name__}: {exc}")


def main() -> None:
    s = get_settings()
    print("=" * 78)
    print("주가 데이터 소스 후보")
    print("=" * 78)
    probe("Yahoo chart 005930.KS", "https://query1.finance.yahoo.com/v8/finance/chart/005930.KS?range=1y&interval=1d")
    probe("Yahoo chart 005930.KS (q2)", "https://query2.finance.yahoo.com/v8/finance/chart/005930.KS?range=1y&interval=1d")
    probe("stooq 005930.ks", "https://stooq.com/q/d/l/?s=005930.ks&i=d")
    probe("naver finance sise", "https://finance.naver.com/item/sise_day.naver?code=005930&page=1")

    print()
    print("=" * 78)
    print("IR 자동수집 후보")
    print("=" * 78)
    probe("DART viewer", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20251231000603")
    probe("DART document.xml", "https://opendart.fss.or.kr/api/document.xml", headers=UA)
    probe("DART corp IR page", "https://dart.fss.or.kr/dsae001/main.do?corpName=%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90")

    print()
    print("=" * 78)
    print("DART document.xml (키 사용)")
    print("=" * 78)
    if s.dart.api_key:
        rcp = "20251231000603"
        url = f"https://opendart.fss.or.kr/api/document.xml?crtfc_key={s.dart.api_key}&rcept_no={rcp}"
        probe("document.xml rcept_no", url)
        for dtd in ("HTML", "TXT", "XML"):
            probe(f"document.xml dtd={dtd}", f"{url}&dtd={dtd}")
    else:
        print("DART 키 없음")


if __name__ == "__main__":
    main()
