"""DART document.xml 원문 + Yahoo 주가 구조 확인."""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.pipeline.parser import html_to_text  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 SignalForge/0.1"}


def main() -> None:
    s = get_settings()
    print("=" * 78)
    print("1) DART document.xml 원문 추출")
    print("=" * 78)
    if not s.dart.api_key:
        print("DART 키 없음")
        return
    rcp = "20251231000603"
    url = (
        f"https://opendart.fss.or.kr/api/document.xml?crtfc_key={s.dart.api_key}"
        f"&rcept_no={rcp}"
    )
    with httpx.Client(timeout=30, follow_redirects=True, headers=UA) as c:
        r = c.get(url)
    print("HTTP", r.status_code, "bytes", len(r.content))
    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
            print("zip entries:", names)
            raw = zf.read(names[0])
            text = raw.decode("utf-8", errors="replace")
            plain = html_to_text(text)
            print("raw len:", len(text), "| plain len:", len(plain))
            print("--- plain head ---")
            print(plain[:700].replace("\n", " ")[:700])
    except Exception as exc:  # noqa: BLE001
        print("ZIP 파싱 실패:", type(exc).__name__, exc)
        print(r.content[:300])

    print()
    print("=" * 78)
    print("2) Yahoo 주가 시계열 구조")
    print("=" * 78)
    u = "https://query1.finance.yahoo.com/v8/finance/chart/005930.KS?range=1y&interval=1d"
    with httpx.Client(timeout=30, follow_redirects=True, headers=UA) as c:
        r = c.get(u)
    data = r.json()
    result = data["chart"]["result"][0]
    meta = result["meta"]
    print("symbol:", meta.get("symbol"), "| currency:", meta.get("currency"))
    print("regularMarketPrice:", meta.get("regularMarketPrice"))
    print("52w high/low:", meta.get("fiftyTwoWeekHigh"), meta.get("fiftyTwoWeekLow"))
    ts = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    print("points:", len(ts), "| first close:", closes[0] if closes else None)
    print("last 5:", list(zip(ts[-5:], closes[-5:])))
    # 주가 시세 -> 날짜 매핑 확인
    import datetime

    for t, c_ in list(zip(ts, closes))[-3:]:
        print("  ", datetime.datetime.utcfromtimestamp(t).date(), c_)


if __name__ == "__main__":
    main()
