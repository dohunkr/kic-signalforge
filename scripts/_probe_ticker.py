"""DART stock_code -> Yahoo 심볼 매핑 확인."""

from __future__ import annotations

import io
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 SignalForge/0.1"}


def main() -> None:
    s = get_settings()
    with httpx.Client(timeout=40, follow_redirects=True, headers=UA) as c:
        r = c.get(
            "https://opendart.fss.or.kr/api/corpCode.xml",
            params={"crtfc_key": s.dart.api_key},
        )
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        xml = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
    block = re.search(r"<list>(.*?)</list>", xml, re.S).group(1)
    print("sample record fields:")
    for tag in ("corp_code", "corp_name", "stock_code", "modify_date", "accn"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
        print(f"  {tag:14s} = {m.group(1) if m else None}")

    # 한글 종목명 -> stock_code 추출
    for name in ("삼성전자", "SK하이닉스", "카카오"):
        found = None
        for m in re.finditer(r"<list>(.*?)</list>", xml, re.S):
            b = m.group(1)
            nm = re.search(r"<corp_name>(.*?)</corp_name>", b, re.S)
            sc = re.search(r"<stock_code>(.*?)</stock_code>", b, re.S)
            if nm and sc and nm.group(1).strip() == name and sc.group(1).strip():
                found = sc.group(1).strip()
                break
        print(f"{name:10s} -> stock_code {found}")
        if found:
            for suffix in (".KS", ".KQ"):
                sym = found + suffix
                u = (
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                    "?range=5d&interval=1d"
                )
                with httpx.Client(timeout=20, follow_redirects=True, headers=UA) as c:
                    rr = c.get(u)
                ok = False
                price = None
                if rr.status_code == 200:
                    try:
                        res = rr.json()["chart"]["result"][0]
                        ok = True
                        price = res["meta"].get("regularMarketPrice")
                        exch = res["meta"].get("fullExchangeName")
                    except Exception:
                        pass
                print(f"    {sym:12s} HTTP {rr.status_code} ok={ok} price={price} exch={exch if ok else '-'}")


if __name__ == "__main__":
    main()
