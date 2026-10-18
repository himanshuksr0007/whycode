"""Graph schema, organized in three layers.

Layer 1 — structure. Boring facts pulled straight out of the repo:
    Commit, File, Symbol, Person, PullRequest, Issue, DocFile

Layer 2 — intent. What the history seems to mean. A Decision only exists
if something backs it (commit, PR thread, issue, doc) — the builder
refuses to write one without evidence. An Observation is churn nobody
explained; it's a lead, not an answer.

Layer 3 — raw text. NOT in Neo4j. Full bodies/comments/docs live in a
local SQLite FTS index (store/rawctx.py); the graph just points at them.

Relationships:
    (Person)-[:AUTHORED]->(Commit | PullRequest | Issue)
    (Commit)-[:TOUCHED {added, removed, status}]->(File)
    (Commit)-[:INTRODUCED_SYMBOL | :MODIFIED_SYMBOL | :REMOVED_SYMBOL]->(Symbol)
    (Symbol)-[:DEFINED_IN]->(File)
    (PullRequest)-[:MERGED_AS]->(Commit)
    (PullRequest)-[:FIXES]->(Issue)
    (Person)-[:COMMENTED_ON]->(PullRequest | Issue)
    (DocFile)-[:MENTIONS]->(File)
    (Decision)-[:RECORDED_IN]->(Commit)
    (Decision)-[:ABOUT]->(File | Symbol)
    (Decision)-[:BASED_ON]->(PullRequest | Issue)
    (Decision)-[:SUPERSEDES {method, confidence}]->(Decision)
    (Observation)-[:ABOUT]->(File)
"""

CONSTRAINTS = [
    "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Commit) REQUIRE c.sha IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (f:File) REQUIRE f.path IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (s:Symbol) REQUIRE s.key IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Person) REQUIRE p.login IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (pr:PullRequest) REQUIRE pr.id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (i:Issue) REQUIRE i.id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (d:Decision) REQUIRE d.id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (o:Observation) REQUIRE o.id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (d:DocFile) REQUIRE d.path IS UNIQUE",
]


def init_schema(session, database: str) -> None:
    for stmt in CONSTRAINTS:
        session.run(stmt)
