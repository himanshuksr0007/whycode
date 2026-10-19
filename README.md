# whycode

Ask a codebase **"why is this like this?"** and get an answer with receipts:
the decision, the commit/PR/issue behind it, who made it, when — and an
honest "I don't know" when the history doesn't say.

## The one flow

```bash
whycode ask "why are retries disabled by default in httpx?"
```

You get back the decision, the evidence chain with dates and links, anything
it superseded, who to ask about it, and a confidence score you can audit.
If the evidence isn't there, it refuses and hands you leads instead of
guessing.

## Quickstart

**1. Start Neo4j** (or point at AuraDB — see Configuration):

```bash
docker compose up -d        # local Neo4j at bolt://localhost:7687
```

**2. Install and configure:**

```bash
pip install -r requirements.txt
pip install -e .               # provides the `whycode` command
cp .env.example .env        # fill in GITHUB_TOKEN, NEO4J_PASSWORD, ...
export $(cat .env | xargs)  # or use direnv / your preferred loader
```

**3. Ingest a repository** (default demo: `encode/httpx`):

```bash
whycode ingest
```

This clones the repo, parses up to `WHYCODE_MAX_COMMITS` commits (files +
function/class/config-key changes), fetches PRs/issues from the GitHub API,
classifies decisions vs observations, builds the Neo4j graph, and indexes
full texts locally.

**4. Ask:**

```bash
whycode ask "why was the reloader implemented this way?"
whycode stats                 # what got ingested
```

**Fast demo** (150 commits, 30 PRs/issues):

```bash
whycode demo
```

## How it works

Three layers, in decreasing order of trust:

1. **Structure** — commits, files, functions, people, PRs, issues, docs as
   a Neo4j graph. Pulled mechanically, no interpretation.
2. **Intent** — `Decision` nodes, created *only* when there's evidence
   (a revert, a `fixes #n`, rationale in the message, a design doc, a real
   PR thread). No evidence, no Decision — unexplained churn becomes an
   `Observation`, which is a lead, never an answer.
3. **Raw text** — full bodies/comments/docs in a local SQLite FTS index.
   The graph points at it instead of storing blobs.

Asking a question: link it to code entities, walk the evidence subgraph,
resolve supersession (explicit reverts, plus heuristic overlap which is
always marked as a guess), score confidence, refuse under the threshold.
The traversal is returned with the answer so you can check the score.
`docs/ARCHITECTURE.md` has the full spec.

Ingestion is idempotent and resumable: completed PRs/issues are tracked in
SQLite, raw GitHub payloads are kept for replay, and a rerun skips what's
done (or use `--force` to redo). Every run writes an ingestion report and a
JSONL dump of the normalized evidence under the data dir. `whycode ask
--json` gives you the answer as JSON, with categorical confidence
(`high`/`medium`/`low`) as the primary signal and the numeric score kept
for audit.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j / AuraDB (`neo4j+s://…`) |
| `NEO4J_USER` / `NEO4J_PASSWORD` | `neo4j` / `password` | auth |
| `GITHUB_TOKEN` | — | raises API limit 60 → 5000/hr |
| `GITHUB_REPO` | `encode/httpx` | `owner/name` to ingest |
| `WHYCODE_MAX_COMMITS` | `1200` | commit cap |
| `WHYCODE_MAX_PRS` / `WHYCODE_MAX_ISSUES` | `150` | API caps |
| `WHYCODE_CONFIDENCE_THRESHOLD` | `0.45` | refusal threshold |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | — | optional answer verbalizer (OpenAI-compatible); unset = deterministic templates |

## Honest limitations

- Symbol extraction uses **tree-sitter** for Python (with a regex fallback
  for other languages and when tree-sitter isn't installed). Still
  approximate — it's for linking, not a compiler.
- Rationale detection is keywords + conventions (`because`, `fixes #n`,
  `Revert`, design docs). Silent decisions stay `Observation`s on purpose.
- Heuristic supersession (word overlap ≥ 0.4) can misfire; it's always
  labeled as a guess with its confidence.
- GitHub ingestion is capped and wants a token for big repos.
- No hosted demo yet — recording goes here: *(todo)*.

## Tests

```bash
python -m unittest discover -s tests   # no Neo4j needed
```

Graph integration is exercised through `whycode demo` against a live Neo4j.

## Layout

```
whycode/ingest/     git history, GitHub API, intent classification + gate,
                    tree-sitter symbols, relevance scoring, ingest state
whycode/graph/      Neo4j schema (3 layers) + batched builder
whycode/store/      raw-context SQLite FTS index
whycode/retrieval/  entity linking + answering (evidence, supersession,
                    confidence, refusal)
whycode/evidence_ids.py  stable content-addressed IDs (repo:type:key)
docs/ARCHITECTURE.md  reusable spec of the above
```
