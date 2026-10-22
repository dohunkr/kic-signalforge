"""실행 중인 서버를 대상으로 한 간단한 HTTP 스모크 테스트.

    python scripts/smoke_http.py [base_url]
"""

from __future__ import annotations

import io
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def get(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as resp:
        return resp.status, resp.read().decode("utf-8")


def post(path: str, payload: dict):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main() -> None:
    status, body = get("/api/health")
    health = json.loads(body)
    print(f"[health] HTTP {status} | 커넥터 상태:")
    for key, cfg in health["connectors"].items():
        print(f"   {key:6s} mode={cfg['mode']:12s} {cfg['note']}")

    status, page = get("/")
    print(f"[index]  HTTP {status} | 길이 {len(page)}")

    payload = {
        "question": "한국은행 기준금리와 원/달러 환율 영향은?",
        "topic": "거시지표",
        "period": "2025",
    }
    status, data = post("/api/research", payload)
    print(
        f"[research] HTTP {status} | run_id={data['run_id']} | 상태={data['status']} | "
        f"출처={len(data['sources'])} | 주장={len(data['claims'])} | "
        f"경고={len(data['uncertainties'])} | 오류={len(data['errors'])}"
    )
    print(f"   LLM: {data['llm']}")
    print("   출처:")
    for source in data["sources"]:
        print(
            f"     - {source['source_id']:26s} | {str(source['published_at']):10s} | "
            f"{str(source['period_label']):12s} | {str(source['unit']):8s} | "
            f"{source['provenance']:6s} | {source['title'][:36]}"
        )
    print("   오류:")
    for err in data["errors"]:
        print(f"     - {err['provider']} [{err['code']}] {err['message'][:80]}")

    status, md = get(f"/api/runs/{data['run_id']}/report.md")
    print(f"[report.md] HTTP {status} | {len(md)}자")
    print("   첫 12줄:")
    for line in md.splitlines()[:12]:
        print("   |", line)


if __name__ == "__main__":
    main()
