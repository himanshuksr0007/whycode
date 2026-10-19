"""Writes everything into Neo4j in batches.

Also the place where the evidence rule is enforced: try to write a
Decision with an empty evidence list and you get an EvidenceGateError.
"""
from __future__ import annotations

from ..evidence_ids import issue_id, pr_id, review_comment_id
from .schema import init_schema

BATCH = 400


class EvidenceGateError(Exception):
    """Raised when a Decision without evidence is submitted for writing."""


class GraphBuilder:
    def __init__(self, driver, database: str = "neo4j"):
        self.driver = driver
        self.database = database

    def _run(self, cypher: str, **params):
        with self.driver.session(database=self.database) as session:
            return session.run(cypher, **params)

    def init_schema(self) -> None:
        with self.driver.session(database=self.database) as session:
            init_schema(session, self.database)

    def wipe(self) -> None:
        self._run("MATCH (n) DETACH DELETE n")

    # ---- structural layer -------------------------------------------------
    def write_commits(self, commits) -> None:
        rows = [
            {
                "sha": c.sha,
                "subject": c.subject,
                "date": c.date,
                "author": c.author,
                "email": c.email,
                "parents": c.parents,
                "fixes": c.fixes_issues,
                "is_revert": c.is_revert,
                "files": [
                    {"path": f.path, "added": f.added,
                     "removed": f.removed, "status": f.status}
                    for f in c.files
                ],
                "symbols": [
                    {"name": s.name, "kind": s.kind, "file": s.file,
                     "change": s.change, "key": f"{s.file}::{s.name}"}
                    for s in c.symbols
                ],
            }
            for c in commits
        ]
        for i in range(0, len(rows), BATCH):
            self._run(
                """
                UNWIND $rows AS r
                MERGE (c:Commit:Structural {sha: r.sha})
                SET c.subject = r.subject, c.date = r.date,
                    c.parents = r.parents, c.is_revert = r.is_revert
                MERGE (p:Person:Structural {login: r.author})
                SET p.email = r.email
                MERGE (p)-[:AUTHORED]->(c)
                WITH c, r
                UNWIND r.files AS f
                MERGE (fl:File:Structural {path: f.path})
                MERGE (c)-[t:TOUCHED]->(fl)
                SET t.added = f.added, t.removed = f.removed, t.status = f.status
                """,
                rows=rows[i:i + BATCH],
            )
        for i in range(0, len(rows), BATCH):
            self._run(
                """
                UNWIND $rows AS r
                MATCH (c:Commit:Structural {sha: r.sha})
                UNWIND r.symbols AS s
                MERGE (fl:File:Structural {path: s.file})
                MERGE (sym:Symbol:Structural {key: s.key})
                SET sym.name = s.name, sym.kind = s.kind
                MERGE (sym)-[:DEFINED_IN]->(fl)
                WITH c, sym, s
                CALL {
                  WITH c, sym, s
                  WITH c, sym WHERE s.change = 'introduced'
                  MERGE (c)-[:INTRODUCED_SYMBOL]->(sym)
                }
                CALL {
                  WITH c, sym, s
                  WITH c, sym WHERE s.change = 'modified'
                  MERGE (c)-[:MODIFIED_SYMBOL]->(sym)
                }
                CALL {
                  WITH c, sym, s
                  WITH c, sym WHERE s.change = 'removed'
                  MERGE (c)-[:REMOVED_SYMBOL]->(sym)
                }
                """,
                rows=rows[i:i + BATCH],
            )

    def write_github(self, prs, issues, rk: str) -> None:
        pr_rows = [
            {
                "id": pr_id(rk, p.number),
                "number": p.number,
                "title": p.title,
                "merged_at": p.merged_at,
                "merge_sha": p.merge_commit_sha,
                "url": p.url,
                "user": p.user,
                "comments": [
                    {"user": cm["user"], "body": cm["body"]}
                    for cm in p.comments
                ],
            }
            for p in prs
        ]
        for i in range(0, len(pr_rows), BATCH):
            self._run(
                """
                UNWIND $rows AS r
                MERGE (pr:PullRequest:Structural {id: r.id})
                SET pr.number = r.number, pr.title = r.title,
                    pr.merged_at = r.merged_at, pr.url = r.url
                MERGE (p:Person:Structural {login: r.user})
                MERGE (p)-[:AUTHORED]->(pr)
                WITH pr, r
                UNWIND r.comments AS cm
                MERGE (cp:Person:Structural {login: cm.user})
                MERGE (cp)-[:COMMENTED_ON]->(pr)
                """,
                rows=pr_rows[i:i + BATCH],
            )
        # merge links + FIXES links need the commits/issues to exist first
        self._run(
            """
            UNWIND $rows AS r
            MATCH (pr:PullRequest:Structural {id: r.id})
            WHERE r.merge_sha IS NOT NULL
            MATCH (c:Commit:Structural {sha: r.merge_sha})
            MERGE (pr)-[:MERGED_AS]->(c)
            """,
            rows=pr_rows,
        )
        issue_rows = [
            {
                "id": issue_id(rk, i.number),
                "number": i.number,
                "title": i.title,
                "closed_at": i.closed_at,
                "url": i.url,
                "user": i.user,
                "comments": [
                    {"user": cm["user"], "body": cm["body"]}
                    for cm in i.comments
                ],
            }
            for i in issues
        ]
        for i in range(0, len(issue_rows), BATCH):
            self._run(
                """
                UNWIND $rows AS r
                MERGE (i:Issue:Structural {id: r.id})
                SET i.number = r.number, i.title = r.title,
                    i.closed_at = r.closed_at, i.url = r.url
                MERGE (p:Person:Structural {login: r.user})
                MERGE (p)-[:AUTHORED]->(i)
                WITH i, r
                UNWIND r.comments AS cm
                MERGE (cp:Person:Structural {login: cm.user})
                MERGE (cp)-[:COMMENTED_ON]->(i)
                """,
                rows=issue_rows[i:i + BATCH],
            )

    def write_review_comments(self, prs, rk: str) -> None:
        """PR review comments anchored to file/line/side."""
        rows = []
        for p in prs:
            pid = pr_id(rk, p.number)
            for rc in p.review_comments:
                rows.append({
                    "id": review_comment_id(rk, rc.id),
                    "pr_id": pid,
                    "path": rc.path,
                    "line": rc.line,
                    "side": rc.side,
                    "url": rc.url,
                    "user": rc.user,
                    "created_at": rc.created_at,
                })
        for i in range(0, len(rows), BATCH):
            self._run(
                """
                UNWIND $rows AS r
                MERGE (rc:ReviewComment:Structural {id: r.id})
                SET rc.path = r.path, rc.line = r.line, rc.side = r.side,
                    rc.url = r.url, rc.created_at = r.created_at
                MERGE (p:Person:Structural {login: r.user})
                MERGE (p)-[:AUTHORED]->(rc)
                WITH rc, r
                MATCH (pr:PullRequest:Structural {id: r.pr_id})
                MERGE (pr)-[:HAS_COMMENT]->(rc)
                WITH rc, r
                MATCH (f:File:Structural {path: r.path})
                MERGE (rc)-[:COMMENTS_ON]->(f)
                """,
                rows=rows[i:i + BATCH],
            )

    def link_fixes(self, decisions) -> None:
        """Connect fix-kind decisions to their issues: PR -[:FIXES]-> Issue."""
        rows = [
            {"sha": d.commit_sha, "issue": d.linked_issue}
            for d in decisions
            if d.kind == "fix" and d.linked_issue and d.commit_sha
        ]
        if rows:
            self._run(
                """
                UNWIND $rows AS r
                MATCH (c:Commit:Structural {sha: r.sha})<-[:MERGED_AS]-(pr:PullRequest)
                MATCH (i:Issue:Structural) WHERE i.number = r.issue
                MERGE (pr)-[:FIXES]->(i)
                """,
                rows=rows,
            )

    def write_docs(self, docs) -> None:
        rows = [{"path": d["path"], "title": d.get("title", "")} for d in docs]
        for i in range(0, len(rows), BATCH):
            self._run(
                """
                UNWIND $rows AS r
                MERGE (d:DocFile:Structural {path: r.path})
                SET d.title = r.title
                """,
                rows=rows[i:i + BATCH],
            )

    # ---- intent layer -----------------------------------------------------
    def write_intent(self, decisions, observations) -> None:
        for d in decisions:
            if not d.evidence:
                raise EvidenceGateError(
                    f"refusing to write Decision {d.id} without evidence"
                )

        def evidence_kind(eid: str) -> str | None:
            # "encode:httpx:pr:123" -> "pr" (repo key itself has a colon)
            parts = eid.split(":")
            return parts[2] if len(parts) > 3 else None

        rows = [
            {
                "id": d.id,
                "kind": d.kind,
                "summary": d.summary,
                "date": d.date,
                "confidence": d.confidence,
                "evidence": d.evidence,
                "commit_sha": d.commit_sha,
                "about_files": d.about_files,
                "about_symbols": d.about_symbols,
                "based_on": [
                    {"kind": evidence_kind(e), "id": e}
                    for e in d.evidence
                    if evidence_kind(e) in ("pr", "issue")
                ],
            }
            for d in decisions
        ]
        for i in range(0, len(rows), BATCH):
            self._run(
                """
                UNWIND $rows AS r
                MERGE (d:Decision:Intent {id: r.id})
                SET d.kind = r.kind, d.summary = r.summary, d.date = r.date,
                    d.confidence = r.confidence, d.evidence = r.evidence
                WITH d, r
                WHERE r.commit_sha IS NOT NULL
                MATCH (c:Commit:Structural {sha: r.commit_sha})
                MERGE (d)-[:RECORDED_IN]->(c)
                """,
                rows=rows[i:i + BATCH],
            )
        for i in range(0, len(rows), BATCH):
            self._run(
                """
                UNWIND $rows AS r
                MATCH (d:Decision:Intent {id: r.id})
                UNWIND r.about_files AS fp
                MATCH (f:File:Structural {path: fp})
                MERGE (d)-[:ABOUT]->(f)
                """,
                rows=rows[i:i + BATCH],
            )
        for i in range(0, len(rows), BATCH):
            self._run(
                """
                UNWIND $rows AS r
                MATCH (d:Decision:Intent {id: r.id})
                UNWIND r.about_symbols AS sk
                MATCH (s:Symbol:Structural {key: sk})
                MERGE (d)-[:ABOUT]->(s)
                """,
                rows=rows[i:i + BATCH],
            )
        for i in range(0, len(rows), BATCH):
            self._run(
                """
                UNWIND $rows AS r
                MATCH (d:Decision:Intent {id: r.id})
                UNWIND r.based_on AS b
                CALL {
                  WITH d, b
                  WITH d, b WHERE b.kind = 'pr'
                  MATCH (pr:PullRequest:Structural {id: b.id})
                  MERGE (d)-[:BASED_ON]->(pr)
                }
                CALL {
                  WITH d, b
                  WITH d, b WHERE b.kind = 'issue'
                  MATCH (iss:Issue:Structural {id: b.id})
                  MERGE (d)-[:BASED_ON]->(iss)
                }
                """,
                rows=rows[i:i + BATCH],
            )
        o_rows = [
            {
                "id": o.id,
                "kind": o.kind,
                "summary": o.summary,
                "about_files": o.about_files,
            }
            for o in observations
        ]
        for i in range(0, len(o_rows), BATCH):
            self._run(
                """
                UNWIND $rows AS r
                MERGE (o:Observation:Intent {id: r.id})
                SET o.kind = r.kind, o.summary = r.summary
                WITH o, r
                UNWIND r.about_files AS fp
                MATCH (f:File:Structural {path: fp})
                MERGE (o)-[:ABOUT]->(f)
                """,
                rows=o_rows[i:i + BATCH],
            )

    def apply_supersession(self, edges) -> None:
        if not edges:
            return
        self._run(
            """
            UNWIND $rows AS r
            MATCH (a:Decision:Intent {id: r.newer}),
                  (b:Decision:Intent {id: r.older})
            MERGE (a)-[s:SUPERSEDES]->(b)
            SET s.method = r.method, s.confidence = r.confidence
            """,
            rows=edges,
        )

    def stats(self) -> dict:
        out = {}
        with self.driver.session(database=self.database) as session:
            for label in ("Commit", "File", "Symbol", "Person", "PullRequest",
                          "Issue", "ReviewComment", "Decision",
                          "Observation", "DocFile"):
                rec = session.run(
                    f"MATCH (n:{label}) RETURN count(n) AS c"
                ).single()
                out[label] = rec["c"]
            rec = session.run(
                "MATCH ()-[s:SUPERSEDES]->() RETURN count(s) AS c"
            ).single()
            out["SupersedesEdges"] = rec["c"]
        return out
