"""Turns a question into graph entities.

Tries, in order:
  1. file paths in the question  -> File nodes (exact, then suffix)
  2. `backticked` / CONSTANTS / CamelCase / snake_case -> Symbol nodes
  3. plain keywords over file paths, as a last resort

Fuzzy hits come back marked match_type='fuzzy' so anything built on them
is tagged INFERENCE downstream.
"""
from __future__ import annotations

import re

PATH_RE = re.compile(
    r"[\w\-./]*[\w\-]/[\w\-.]*\.(?:py|js|ts|tsx|jsx|java|kt|go|rs|c|h|cpp|"
    r"yaml|yml|toml|json|md|rst|txt|cfg|ini|mk)"
)
BACKTICK_RE = re.compile(r"`([^`]{2,60})`")
UPPER_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
CAMEL_RE = re.compile(r"\b(?:[a-z]+(?:[A-Z][a-z0-9]+)+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
SNAKE_RE = re.compile(r"\b[a-z][a-z0-9_]{3,}\b")
STOP = {
    "what", "whats", "why", "does", "this", "that", "with", "from", "have",
    "there", "their", "about", "into", "code", "file", "used", "using",
}


def extract_candidates(question: str) -> dict:
    paths = list(dict.fromkeys(PATH_RE.findall(question)))
    backticked = [b for b in BACKTICK_RE.findall(question) if b not in paths]
    symbols = list(
        dict.fromkeys(
            t for t in UPPER_RE.findall(question)
            + CAMEL_RE.findall(question)
            + SNAKE_RE.findall(question)
            + backticked
            if t.lower() not in STOP
        )
    )
    keywords = [
        w for w in re.findall(r"[a-zA-Z]{4,}", question.lower())
        if w not in STOP
    ]
    return {"paths": paths, "symbols": symbols, "keywords": keywords}


def link_entities(driver, database: str, candidates: dict) -> list[dict]:
    """Return ranked entity hits: [{kind, key, path/name, match_type}]."""
    hits: list[dict] = []
    seen = set()

    def add(kind, key, label, match_type):
        if (kind, key) not in seen:
            seen.add((kind, key))
            hits.append({"kind": kind, "key": key,
                         "label": label, "match_type": match_type})

    with driver.session(database=database) as session:
        for p in candidates["paths"]:
            row = session.run(
                "MATCH (f:File:Structural) WHERE f.path = $p "
                "RETURN f.path AS path LIMIT 1",
                p=p,
            ).single()
            if row:
                add("File", row["path"], row["path"], "exact")
                continue
            row = session.run(
                "MATCH (f:File:Structural) WHERE f.path ENDS WITH $s "
                "RETURN f.path AS path LIMIT 1",
                s="/" + p.lstrip("./"),
            ).single()
            if row:
                add("File", row["path"], row["path"], "partial")

        for t in candidates["symbols"][:12]:
            rows = session.run(
                "MATCH (s:Symbol:Structural) "
                "WHERE toLower(s.name) = toLower($t) "
                "RETURN s.key AS key, s.name AS name LIMIT 5",
                t=t,
            )
            exact = list(rows)
            if exact:
                for r in exact:
                    add("Symbol", r["key"], r["name"], "exact")
            else:
                rows = session.run(
                    "MATCH (s:Symbol:Structural) "
                    "WHERE toLower(s.name) CONTAINS toLower($t) "
                    "RETURN s.key AS key, s.name AS name LIMIT 5",
                    t=t,
                )
                for r in rows:
                    add("Symbol", r["key"], r["name"], "fuzzy")

        if not hits:
            for kw in candidates["keywords"][:6]:
                rows = session.run(
                    "MATCH (f:File:Structural) "
                    "WHERE toLower(f.path) CONTAINS toLower($kw) "
                    "RETURN f.path AS path LIMIT 5",
                    kw=kw,
                )
                for r in rows:
                    add("File", r["path"], r["path"], "fuzzy")
    return hits
