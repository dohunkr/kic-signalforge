"""전방(Outlook) 분류 과정 직접 확인."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.config import load_settings  # noqa: E402
from app.models import ResearchRequest  # noqa: E402
from app.pipeline import outlook as outlook_mod  # noqa: E402
from app.pipeline.orchestrator import Orchestrator  # noqa: E402

s = load_settings()
o = Orchestrator(s)
b = o.run(
    ResearchRequest(
        question="2025년 삼성전자 실적 전망 근거는?", company="삼성전자", period="2025"
    )
)
print("claims:", len(b.claims))
print("outlooks:", len(b.claim_outlooks))
from collections import Counter

print("directions:", Counter(o.direction.value for o in b.claim_outlooks))
print("methods:", Counter(o.method for o in b.claim_outlooks))
for o in b.claim_outlooks[:8]:
    print("  ", o.claim_id, o.direction.value, o.strength, o.method, "|", o.rationale[:60])
print()
print("summary:", json.dumps(b.outlook.model_dump(), ensure_ascii=False)[:600])
print()
print("claim metric counts:", [(c.claim_id, len(c.metrics)) for c in b.claims[:12]])
