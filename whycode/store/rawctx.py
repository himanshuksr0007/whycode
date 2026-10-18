"""Full texts go here, not in Neo4j.

Commits bodies, PR/issue threads, doc contents — indexed with SQLite FTS
so answers can quote the exact words. The graph keeps the structure and
points here by reference instead of carrying blobs around.
"""
from __future__ import annotations

import os
import sqlite3


class RawContext:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS texts "
            "USING fts5(id, kind, ref, title, body)"
        )
        self.conn.commit()

    def add(self, id: str, kind: str, ref: str, title: str, body: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO texts (id, kind, ref, title, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (id, kind, ref, title or "", (body or "")[:20000]),
        )

    def commit(self) -> None:
        self.conn.commit()

    def search(self, query: str, limit: int = 8) -> list[dict]:
        """Keyword search returning snippets with match offsets."""
        # FTS5 query sanitization: quote terms, drop operators.
        terms = [t.strip('"') for t in query.split() if len(t) > 2]
        if not terms:
            return []
        q = " OR ".join(f'"{t}"' for t in terms[:8])
        rows = self.conn.execute(
            "SELECT id, kind, ref, title, "
            "snippet(texts, 4, '[', ']', '...', 30) AS snip "
            "FROM texts WHERE texts MATCH ? LIMIT ?",
            (q, limit),
        ).fetchall()
        return [
            {"id": r[0], "kind": r[1], "ref": r[2], "title": r[3], "snippet": r[4]}
            for r in rows
        ]

    def get(self, id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, kind, ref, title, body FROM texts WHERE id = ?", (id,)
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "kind": row[1], "ref": row[2],
                "title": row[3], "body": row[4]}

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
