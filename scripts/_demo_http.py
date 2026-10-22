"""새 UI 흐름 HTTP 검증: 채팅 → 잔고 → 결과 + 시뮬레이션."""

from __future__ import annotations

import io
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return r.status, r.read().decode("utf-8")


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=240) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main() -> None:
    print("=" * 78)
    print("1) 정적 페이지")
    print("=" * 78)
    for path in ("/", "/static/styles.css", "/static/app.js"):
        status, body = get(path)
        print(f"   {path:22s} HTTP {status} | {len(body):>7}B")

    print()
    print("=" * 78)
    print("2) 헬스체크")
    print("=" * 78)
    _, health_raw = get("/api/health")
    health = json.loads(health_raw)
    for key, cfg in (health.get("connectors") or {}).items():
        print(f"   {key:6s} mode={cfg['mode']:12s} {cfg.get('note','')[:44]}")

    print()
    print("=" * 78)
    print("3) 대화 흐름")
    print("=" * 78)
    started = time.time()
    _, reply = post("/api/chat", {"message": ""})
    print(f"   [welcome] step={reply['step']}")
    print(f"      {reply['reply'].splitlines()[0]}")
    print(f"      예시 {reply['quick_replies'][:2]}")

    _, reply = post("/api/chat", {"session_id": reply["session_id"], "message": "2025년 삼성전자 실적 전망 근거는?"})
    print(f"   [question] step={reply['step']}")
    print(f"      {reply['reply'].splitlines()[0]}")

    _, reply = post("/api/chat", {"session_id": reply["session_id"], "message": "1,000만원"})
    print(f"   [balance]  step={reply['step']}")
    for line in reply["reply"].splitlines():
        print("      " + line)

    state = reply.get("state") or {}
    result = state.get("result") or {}
    briefing = result.get("briefing") or {}
    simulation = result.get("simulation") or {}
    outlook = briefing.get("outlook") or {}

    print()
    print("   --- 전망 ---")
    print(
        "   상승 %.0f%% / 하락 %.0f%% | 기대수익률 %+.2f%% | 신뢰도 %.0f%%"
        % (
            outlook.get("probability_up", 0) * 100,
            outlook.get("probability_down", 0) * 100,
            outlook.get("expected_return", 0),
            outlook.get("confidence", 0) * 100,
        )
    )
    print(f"   출처 {len(briefing.get('sources') or [])}건 | 주장 {len(briefing.get('claims') or [])}건")

    print()
    print("   --- 시뮬레이션 ---")
    if simulation:
        print(f"   {simulation['symbol']} 현재가 {simulation['current_price']:,.0f} {simulation['currency']}")
        for sc in simulation["scenarios"]:
            print(
                f"     {sc['label']:12s} 확률 {sc['probability']:.0%} "
                f"최종 {sc['final_price']:,.0f} ({sc['return_pct']:+.2f}%) 점 {len(sc['points'])}개"
            )
        pts = simulation.get("outlook_points") or []
        if pts:
            print(f"   hover 포인트 {len(pts)}개 · 예시:")
            for pt in pts[:2]:
                print(f"     {pt['date']} {pt['price']:,.0f} {pt['cumulative_return']:+.1f}% | {pt['outlook']}")
                if pt.get("evidence"):
                    print(f"        근거: {pt['evidence'][0][:76]}")
    else:
        print("   (없음)")
    print(f"\n   총 소요 {time.time() - started:.1f}s")

    print()
    print("=" * 78)
    print("4) simulate 단독 호출")
    print("=" * 78)
    _, data = post("/api/simulate", {"company": "삼성전자", "balance": 10000000})
    sim = data.get("simulation")
    print(f"   -> {sim['symbol'] if sim else '없음'} | 현재가 {sim['current_price'] if sim else '-'}")


if __name__ == "__main__":
    main()
