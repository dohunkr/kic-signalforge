"""대화 플로우 + 시뮬레이션 실제 동작 확인."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.chat.session import ChatService, parse_amount  # noqa: E402
from app.service import ResearchService  # noqa: E402


def main() -> None:
    print("=" * 78)
    print("1) 금액 파싱")
    print("=" * 78)
    for text in ("1,000만원", "5000000", "1억원", "3천만원", "abc"):
        print(f"   {text!r:14s} -> {parse_amount(text)}")

    print()
    print("=" * 78)
    print("2) 대화 플로우 (실제 조사 + 시뮬레이션)")
    print("=" * 78)
    service = ResearchService()
    chat = ChatService(service)

    reply = chat.welcome(chat.session())
    print("   [WELCOME]", reply.reply.splitlines()[0])
    print("   예시:", reply.quick_replies[:2])

    reply = chat.handle(reply.session_id, "2025년 삼성전자 반도체 실적 전망 근거는?")
    print("   [STEP]", reply.step.value)
    print("   [ASK]", reply.reply.splitlines()[0])

    reply = chat.handle(reply.session_id, "1,000만원")
    print("   [STEP]", reply.step.value)
    print("   [RESULT]", reply.reply.replace("\n", " | "))

    result = reply.state.get("result") or {}
    briefing = result.get("briefing") or {}
    simulation = result.get("simulation") or {}
    outlook = briefing.get("outlook") or {}
    print()
    print("   출처:", len(briefing.get("sources") or []), "| 주장:", len(briefing.get("claims") or []))
    print(
        "   전망: 상승 %.0f%% / 하락 %.0f%% | 기대수익률 %+.2f%% | 신뢰도 %.0f%%"
        % (
            outlook.get("probability_up", 0) * 100,
            outlook.get("probability_down", 0) * 100,
            outlook.get("expected_return", 0),
            outlook.get("confidence", 0) * 100,
        )
    )
    if outlook.get("bull_evidence"):
        print("   상승 근거:", outlook["bull_evidence"][0][:90])
    if outlook.get("bear_evidence"):
        print("   하락 근거:", outlook["bear_evidence"][0][:90])
    if simulation:
        print()
        print("   시뮬레이션:", simulation.get("symbol"), "| 현재가", simulation.get("current_price"))
        print("   과거 포인트:", len(simulation.get("history") or []))
        for sc in simulation.get("scenarios") or []:
            print(
                f"     {sc['label']:12s} 확률 {sc['probability']:.0%} "
                f"최종 {sc['final_price']:,.0f} ({sc['return_pct']:+.2f}%)"
            )
        pts = simulation.get("outlook_points") or []
        if pts:
            print("   hover 포인트 예시:")
            for pt in pts[:3]:
                print(f"     {pt['date']} {pt['price']:,.0f} {pt['cumulative_return']:+.1f}% | {pt['outlook']}")
                if pt.get("evidence"):
                    print(f"        근거: {pt['evidence'][0][:80]}")
    else:
        print("   (시뮬레이션 없음)")


if __name__ == "__main__":
    main()
