"""세션 단위 SQLite 저장소.

- 브리핑(JSON)과 사용된 출처 메타데이터만 보관한다.
- API 키·개인정보는 저장하지 않는다.
- 장기 Memory 는 필요해질 때 확장한다(MVP 범위 밖).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import Briefing, SourceDoc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS briefings (
    run_id        TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    question     TEXT NOT NULL,
    topic        TEXT,
    period       TEXT,
    status       TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id     TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    provider      TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    published_at  TEXT,
    period_label  TEXT,
    unit          TEXT,
    provenance    TEXT NOT NULL,
    retrieved_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_briefings_created ON briefings(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sources_run ON sources(run_id);
"""


class SessionStore:
    """SQLite 기반 세션 저장소."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    # --- 내부 -----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    # --- 쓰기 -----------------------------------------------------------
    def save_briefing(self, briefing: Briefing) -> None:
        payload = briefing.model_dump(mode="json")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO briefings
                    (run_id, created_at, question, topic, period, status, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    briefing.run_id,
                    briefing.stats.started_at.isoformat(),
                    briefing.question,
                    briefing.topic,
                    briefing.period,
                    briefing.status.value,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            conn.execute("DELETE FROM sources WHERE run_id = ?", (briefing.run_id,))
            conn.executemany(
                """
                INSERT OR REPLACE INTO sources
                    (source_id, run_id, provider, title, url, published_at,
                     period_label, unit, provenance, retrieved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        s.source_id,
                        briefing.run_id,
                        s.provider.value,
                        s.title,
                        s.url,
                        s.published_at.isoformat() if s.published_at else None,
                        s.period_label,
                        s.unit,
                        s.provenance.value,
                        s.retrieved_at.isoformat(),
                    )
                    for s in briefing.sources
                ],
            )

    # --- 읽기 -----------------------------------------------------------
    def get_briefing(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM briefings WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"])

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, created_at, question, topic, period, status
                FROM briefings ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_sources(self, run_id: str) -> List[SourceDoc]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sources WHERE run_id = ?", (run_id,)
            ).fetchall()
        return [
            SourceDoc(
                source_id=row["source_id"],
                provider=row["provider"],
                title=row["title"],
                url=row["url"],
                provenance=row["provenance"],
                retrieved_at=row["retrieved_at"],
                published_at=row["published_at"],
                period_label=row["period_label"],
                unit=row["unit"],
            )
            for row in rows
        ]
