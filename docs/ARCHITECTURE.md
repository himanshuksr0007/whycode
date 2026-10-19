# Architecture

whycode answers "why is this code like this?" from a repo's own history.
The whole design is three layers with decreasing trust.

## Evidence IDs

Everything gets a stable, content-addressed ID: `{repo_key}:{type}:{key}`,
e.g. `encode:httpx:pr:123`, `encode:httpx:commit:abc…`, `encode:httpx:review_comment:456`.
No UUIDs, no counters — reruns produce identical IDs, which is what makes
ingestion idempotent. (`whycode/evidence_ids.py`)

## 1. Structure — boring facts

Pulled mechanically out of the repo. No interpretation.

- Nodes: `Commit`, `File`, `Symbol` (function/class/import/config key),
  `Person`, `PullRequest`, `Issue`, `ReviewComment`, `DocFile`
- Edges: `AUTHORED`, `TOUCHED`, `INTRODUCED/MODIFIED/REMOVED_SYMBOL`,
  `DEFINED_IN`, `MERGED_AS`, `FIXES`, `HAS_COMMENT`, `COMMENTS_ON`,
  `COMMENTED_ON`, `MENTIONS`
- Built by: `ingest/git_history.py`, `ingest/github_api.py`, `graph/builder.py`
- Symbols come from **tree-sitter** for Python (regex fallback for other
  languages / when tree-sitter is missing). Classification into
  introduced/removed/modified uses blob-cached parses plus diff-hunk
  overlap — a symbol is "modified" only if a hunk touches its lines.

## 2. Intent — what the history seems to mean

A `Decision` node has a `kind` (`revert | fix | rationale | doc |
pr_rationale`), a summary, a date, and — mandatory — an `evidence` list
pointing at the commit / PR / issue / doc behind it.

The rule: no evidence, no Decision. `ingest/intent.py` returns nothing for
commits with no rationale signal, and the builder throws
`EvidenceGateError` if you try to write a Decision with an empty evidence
list anyway.

Decisions carry a **status**: `active`, `superseded` (explicitly replaced
by a revert), or `contested` (a later decision probably replaced it —
heuristic). Never silently overwritten.

Churn nobody explained becomes an `Observation` (e.g. "changed in 12
commits, zero recorded rationale"). Observations show up as leads. They are
never presented as answers.

Supersession is a `SUPERSEDES` edge between decisions:
- explicit, confidence 1.0 — `Revert "..."` names its target
- heuristic — a later decision with ≥0.4 word overlap on the same file.
  Always surfaced as a guess, with its confidence attached.

High-signal PRs (rationale language, discussion volume, review comments,
linked issues) are scored by `ingest/relevance.py` so decision extraction
can start with the PRs most likely to contain one.

## 3. Raw text — indexed, not stored in the graph

Commit bodies, PR/issue threads, review comments, doc contents live in a
local SQLite FTS index (`store/rawctx.py`), one file per repo. The graph
references them; answers quote snippets from here.

## Ingestion: idempotent and resumable

`ingest/state.py` tracks completed PRs/issues per repo and keeps the raw
GitHub payloads for replay. A rerun skips what's done; `--force` redoes
it. Every run writes an ingestion report and a JSONL dump of the
normalized evidence (the component boundary) under the data dir —
debugging doesn't need Neo4j.

## Asking a question

`whycode ask "why ..."` (`--json` for the machine-readable form):

1. **Link** the question to entities (`retrieval/linking.py`): file paths,
   symbols, config keys. Exact match → suffix → fuzzy → keyword fallback.
   Fuzzy hits are marked so claims built on them are tagged as guesses.
2. **Walk** the evidence subgraph: the entity's decisions, their commits,
   authors, PRs, issues, docs, supersession edges. The Cypher and the node
   ids walked come back with the answer, so the confidence score is
   checkable.
3. **Resolve supersession**: superseded decisions are shown but labeled;
   the newest live decision is the answer.
4. **Score confidence**: decision 0.30, commit 0.10, discussion 0.20, doc
   0.15, person 0.10, corroboration 0.10, recency 0.05. Fuzzy entity links
   cap at 0.60. Under the threshold (default 0.45) the engine refuses and
   offers leads instead. The primary signal is categorical —
   `high`/`medium`/`low` — with the number kept for audit.
5. **Say it**: templates by default; an optional OpenAI-compatible LLM
   rewrite if you configure one (labeled, never adds facts).

## Claim tags

- `[EVIDENCE]` — from the graph or the text index
- `[INFERENCE]` — heuristic output (fuzzy match, guessed supersession)
- anything else doesn't get said

## Tweaking it

- New repo: `GITHUB_REPO=owner/name whycode ingest`
- AuraDB instead of local: set `NEO4J_URI` to the `neo4j+s://` URI
- Heuristics (symbol regexes, rationale keywords, 0.4 overlap, confidence
  weights in `retrieval/qa.py::WEIGHTS`, relevance weights in
  `ingest/relevance.py`) are constants at the top of their modules —
  tune per repo
