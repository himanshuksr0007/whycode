"""Ingestion state: resume, idempotency, and raw payload preservation.

Two tables in one SQLite file:
  ingestion_state  — which PRs/issues are done, so reruns skip them
  raw_payloads     — the raw GitHub JSON, kept separately for replay/debug

If the process dies halfway, the next run picks up where it left off.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IngestState:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS ingestion_state ("
            "repo TEXT NOT NULL, item_type TEXT NOT NULL, "
            "item_id TEXT NOT NULL, status TEXT NOT NULL, "
            "completed_at TEXT, error TEXT, "
            "PRIMARY KEY (repo, item_type, item_id))"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_payloads ("
            "source_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, "
            "payload TEXT NOT NULL, fetched_at TEXT NOT NULL)"
        )
        self.conn.commit()

    def is_complete(self, repo: str, item_type: str, item_id: str) -> bool:
        row = self.conn.execute(
            "SELECT status FROM ingestion_state "
            "WHERE repo=? AND item_type=? AND item_id=?",
            (repo, item_type, str(item_id)),
        ).fetchone()
        return row is not None and row[0] == "complete"

    def mark_complete(self, repo: str, item_type: str, item_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ingestion_state "
            "(repo, item_type, item_id, status, completed_at, error) "
            "VALUES (?, ?, ?, 'complete', ?, NULL)",
            (repo, item_type, str(item_id), _now()),
        )
        self.conn.commit()

    def mark_failed(self, repo: str, item_type: str, item_id: str,
                    error: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ingestion_state "
            "(repo, item_type, item_id, status, completed_at, error) "
            "VALUES (?, ?, ?, 'failed', ?, ?)",
            (repo, item_type, str(item_id), _now(), error[:500]),
        )
        self.conn.commit()

    def save_raw(self, source_id: str, endpoint: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO raw_payloads "
            "(source_id, endpoint, payload, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (source_id, endpoint, json.dumps(payload), _now()),
        )

    def completed_ids(self, repo: str, item_type: str) -> set[int]:
        rows = self.conn.execute(
            "SELECT item_id FROM ingestion_state "
            "WHERE repo=? AND item_type=? AND status='complete'",
            (repo, item_type),
        ).fetchall()
        return {int(r[0]) for r in rows}

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
