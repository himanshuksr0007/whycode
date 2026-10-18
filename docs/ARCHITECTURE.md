# Architecture

whycode answers "why is this code like this?" from a repo's own history.
The whole design is three layers with decreasing trust.

## 1. Structure — boring facts

Pulled mechanically out of the repo. No interpretation.

- Nodes: `Commit`, `File`, `Symbol` (function/class/config key), `Person`,
  `PullRequest`, `Issue`, `DocFile`
- Edges: `AUTHORED`, `TOUCHED`, `INTRODUCED/MODIFIED/REMOVED_SYMBOL`,
  `DEFINED_IN`, `MERGED_AS`, `FIXES`, `COMMENTED_ON`, `MENTIONS`
- Built by: `ingest/git_history.py`, `ingest/github_api.py`, `graph/builder.py`

## 2. Intent — what the history seems to mean

A `Decision` node has a `kind` (`revert | fix | rationale | doc |
pr_rationale`), a summary, a date, and — mandatory — an `evidence` list
pointing at the commit / PR / issue / doc behind it.

The rule: no evidence, no Decision. `ingest/intent.py` returns nothing for
commits with no rationale signal, and the builder throws
`EvidenceGateError` if you try to write a Decision with an empty evidence
list anyway.

Churn nobody explained becomes an `Observation` (e.g. "changed in 12
commits, zero recorded rationale"). Observations show up as leads. They are
never presented as answers.

Supersession is a `SUPERSEDES` edge between decisions:
- explicit, confidence 1.0 — `Revert "..."` names its target
- heuristic — a later decision with ≥0.4 word overlap on the same file.
  Always surfaced as a guess, with its confidence attached.

## 3. Raw text — indexed, not stored in the graph

Commit bodies, PR/issue threads, doc contents live in a local SQLite FTS
index (`store/rawctx.py`), one file per repo. The graph references them;
answers quote snippets from here.

## Asking a question

`whycode ask "why ..."`:

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
   offers leads instead.
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
  weights in `retrieval/qa.py::WEIGHTS`) are constants at the top of their
  modules — tune per repo
