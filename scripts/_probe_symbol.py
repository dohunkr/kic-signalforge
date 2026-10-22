"""Yahoo 종목 식별/마켓 판별 방법 확인."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 SignalForge/0.1"}


def show(label, url):
    with httpx.Client(timeout=20, follow_redirects=True, headers=UA) as c:
        r = c.get(url)
    print(f"--- {label} HTTP {r.status_code}")
    try:
        d = r.json()
    except Exception:
        print(r.text[:200])
        return
    if "chart" in d:
        res = (d["chart"].get("result") or [{}])[0]
        meta = res.get("meta", {})
        print(
            "   symbol={symbol} shortName={shortName} longName={longName} "
            "exch={fullExchangeName} price={regularMarketPrice}".format(
                symbol=meta.get("symbol"),
                shortName=meta.get("shortName"),
                longName=meta.get("longName"),
                fullExchangeName=meta.get("fullExchangeName"),
                regularMarketPrice=meta.get("regularMarketPrice"),
            )
        )
    else:
        for q in (d.get("quotes") or [])[:5]:
            print(
                "   {symbol:14s} {shortname:26s} exch={exchange:10s} type={quoteType} "
                "code={code}".format(
                    symbol=q.get("symbol"),
                    shortname=str(q.get("shortname") or q.get("longname") or "")[:26],
                    exchange=q.get("exchange"),
                    quoteType=q.get("quoteType"),
                    code=q.get("code"),
                )
            )


def main() -> None:
    for sym in ("005930.KS", "005930.KQ", "000660.KS", "035720.KS"):
        show(f"chart {sym}", f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d")
    for q in ("005930", "삼성전자", "000660"):
        show(f"search {q}", f"https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=6")


if __name__ == "__main__":
    main()
