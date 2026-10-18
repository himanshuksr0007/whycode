"""Figures out what counts as a decision.

The rule is simple: if we can't point at evidence, it's not a decision.
A commit that just changes code with no rationale goes in as an Observation
(or nothing at all) — we'd rather say "don't know" than make something up.

A Decision only gets created with a non-empty evidence list: a commit, a PR
thread, a linked issue, a design doc. The graph builder enforces this and
refuses to write Decision nodes without evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

RATIONALE_PATTERNS = [
    r"\bbecause\b",
    r"\bwhy\b",
    r"\brationale\b",
    r"we (chose|decided|picked|went with|opted for)",
    r"instead of",
    r"trade-?offs?",
    r"\bdecision\b",
    r"\badr\b",
]

DOC_PATH_PREFIXES = ("docs/", "doc/", "adr/", "design/", "rfc/", "proposals/")
DOC_NAME_HINTS = ("adr", "design", "decision", "rationale", "proposal", "rfc")

STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "using",
    "use", "used", "add", "added", "fix", "fixed", "update", "updated",
    "remove", "removed", "change", "changed", "are", "was", "were", "has",
    "have", "had", "not", "but", "when", "which", "their", "they", "them",
}


@dataclass
class Decision:
    id: str
    kind: str  # revert | fix | rationale | doc | pr_rationale
    summary: str
    date: str
    evidence: list  # mandatory, non-empty: ["commit:<sha>", "pr:<n>", ...]
    confidence: float  # classifier confidence, 0..1
    commit_sha: str | None = None
    about_files: list = field(default_factory=list)
    about_symbols: list = field(default_factory=list)  # "path::name"
    supersedes_sha: str | None = None  # explicit revert target
    linked_issue: int | None = None


@dataclass
class Observation:
    id: str
    kind: str  # unexplained_hotspot | vague_churn
    summary: str
    date: str
    about_files: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def _has_rationale(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in RATIONALE_PATTERNS)


def _is_doc_path(path: str) -> bool:
    low = path.lower()
    return low.startswith(DOC_PATH_PREFIXES) or any(
        h in low for h in DOC_NAME_HINTS
    )


def classify_commit(rec) -> Decision | None:
    """Return a Decision, or None if the commit doesn't record one.

    None is the gate doing its job: the commit still lands in the graph,
    but nothing will claim it explains the why.
    """
    text = f"{rec.subject}\n{rec.body}"
    files = [f.path for f in rec.files]
    symbols = [f"{s.file}::{s.name}" for s in rec.symbols]
    evidence = [f"commit:{rec.sha}"]

    if rec.is_revert:
        return Decision(
            id=f"d:{rec.sha}",
            kind="revert",
            summary=rec.subject,
            date=rec.date,
            evidence=evidence,
            confidence=0.95,
            commit_sha=rec.sha,
            about_files=files,
            about_symbols=symbols,
            supersedes_sha=rec.revert_of_sha,
        )
    if rec.fixes_issues:
        return Decision(
            id=f"d:{rec.sha}",
            kind="fix",
            summary=rec.subject,
            date=rec.date,
            evidence=evidence,
            confidence=0.7,
            commit_sha=rec.sha,
            about_files=files,
            about_symbols=symbols,
            linked_issue=rec.fixes_issues[0],
        )
    if any(_is_doc_path(p) for p in files):
        return Decision(
            id=f"d:{rec.sha}",
            kind="doc",
            summary=rec.subject,
            date=rec.date,
            evidence=evidence,
            confidence=0.6,
            commit_sha=rec.sha,
            about_files=files,
            about_symbols=symbols,
        )
    if _has_rationale(text):
        return Decision(
            id=f"d:{rec.sha}",
            kind="rationale",
            summary=rec.subject,
            date=rec.date,
            evidence=evidence,
            confidence=0.75,
            commit_sha=rec.sha,
            about_files=files,
            about_symbols=symbols,
        )
    return None


def classify_pr(pr) -> Decision | None:
    """A merged PR with a rationale-bearing body counts as a decision."""
    if not pr.merged_at:
        return None
    text = f"{pr.title}\n{pr.body}"
    if not _has_rationale(text) and len(pr.comments) < 2:
        return None
    evidence = [f"pr:{pr.number}"]
    if pr.merge_commit_sha:
        evidence.append(f"commit:{pr.merge_commit_sha}")
    return Decision(
        id=f"d:pr-{pr.number}",
        kind="pr_rationale",
        summary=pr.title,
        date=pr.merged_at,
        evidence=evidence,
        confidence=0.65,
        commit_sha=pr.merge_commit_sha,
    )


def derive_observations(file_stats: dict[str, dict]) -> list[Observation]:
    """Aggregate observations. file_stats: path -> {commits, decisions}."""
    observations = []
    for path, st in file_stats.items():
        if st["commits"] >= 8 and st["decisions"] == 0:
            observations.append(
                Observation(
                    id=f"o:hotspot:{path}",
                    kind="unexplained_hotspot",
                    summary=(
                        f"{path} changed in {st['commits']} commits with no "
                        "recorded rationale"
                    ),
                    date="",
                    about_files=[path],
                    detail={"commits": st["commits"], "decisions": 0},
                )
            )
    return observations


def token_set(text: str) -> set:
    return set(re.findall(r"[a-z]{3,}", text.lower())) - STOPWORDS


def supersession_edges(decisions: list[Decision],
                       order: dict | None = None) -> list[dict]:
    """Supersession edges between decisions.

    - explicit: a revert names its target (confidence 1.0)
    - heuristic: a later decision with heavily overlapping wording on the
      same file probably replaced the earlier one. Shown as INFERENCE
      downstream, take with salt.

    `order` maps decision id -> sequence number (0 = oldest). Git dates only
    have 1-second resolution, so without it quick successive commits tie and
    the arrow can point the wrong way.
    """
    edges = []
    by_id = {d.id: d for d in decisions}

    for d in decisions:
        if d.supersedes_sha:
            target = by_id.get(f"d:{d.supersedes_sha}")
            if target:
                edges.append(
                    {
                        "newer": d.id,
                        "older": target.id,
                        "method": "explicit",
                        "confidence": 1.0,
                    }
                )

    # Heuristic: group by primary file, order by date, link overlapping pairs.
    by_file: dict[str, list[Decision]] = {}
    for d in decisions:
        for f in d.about_files[:3]:
            by_file.setdefault(f, []).append(d)
    for f, decs in by_file.items():
        def _key(d):
            seq = order.get(d.id, 0) if order else 0
            return (d.date, seq)

        decs = sorted(decs, key=_key)
        for i, older in enumerate(decs):
            toks_o = token_set(older.summary)
            if not toks_o:
                continue
            for newer in decs[i + 1:]:
                toks_n = token_set(newer.summary)
                if not toks_n:
                    continue
                overlap = len(toks_o & toks_n) / min(len(toks_o), len(toks_n))
                if overlap >= 0.4:
                    edges.append(
                        {
                            "newer": newer.id,
                            "older": older.id,
                            "method": "heuristic",
                            "confidence": round(overlap, 2),
                        }
                    )
    # De-duplicate, keeping the highest-confidence edge per pair.
    best = {}
    for e in edges:
        key = (e["newer"], e["older"])
        if key not in best or e["confidence"] > best[key]["confidence"]:
            best[key] = e
    return list(best.values())
