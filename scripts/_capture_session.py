"""결과 화면 스크린샷용: 대화 흐름 실행 후 세션 id를 저장."""

from __future__ import annotations

import io
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
QUESTION = "2025년 삼성전자 실적 전망 근거는?"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    reply = post("/api/chat", {"message": QUESTION})
    reply = post("/api/chat", {"session_id": reply["session_id"], "message": "1,000만원"})
    session_id = reply["session_id"]
    state = reply.get("state") or {}
    result = state.get("result") or {}
    briefing = result.get("briefing") or {}
    simulation = result.get("simulation") or {}
    outlook = briefing.get("outlook") or {}
    print("session_id:", session_id)
    print("status:", briefing.get("status"))
    print(
        "outlook: up=%.0f%% down=%.0f%% ret=%+.2f%% conf=%.0f%%"
        % (
            outlook.get("probability_up", 0) * 100,
            outlook.get("probability_down", 0) * 100,
            outlook.get("expected_return", 0),
            outlook.get("confidence", 0) * 100,
        )
    )
    print("sources:", len(briefing.get("sources") or []), "claims:", len(briefing.get("claims") or []))
    if simulation:
        print("simulation:", simulation.get("symbol"), simulation.get("current_price"))
        print("scenarios:", [(s["label"], s["return_pct"]) for s in simulation.get("scenarios", [])])
    out = Path(__file__).resolve().parent.parent / "data" / "last_session.txt"
    out.write_text(session_id, encoding="utf-8")
    print("saved ->", out)


if __name__ == "__main__":
    main()
