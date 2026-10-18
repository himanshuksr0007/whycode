"""Reads git history into plain records (no Neo4j here).

Two passes over `git log`:
  1. metadata + numstat (fast, one stream)
  2. unified-0 diffs, to pull out changed functions/classes/config keys

The symbol extraction is regex-based, not a real parser. Good enough for
the common cases, wrong on weird syntax — it's marked heuristic everywhere
it gets used.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

FIELD_SEP = "\x1f"
REC_SEP = "\x1e"
SHA_RE = re.compile(r"^[0-9a-f]{40}" + FIELD_SEP)

# Heuristic symbol patterns per extension: (regex, kind).
SYMBOL_PATTERNS: dict[str, list[tuple[str, str]]] = {
    ".py": [
        (r"^\s*def\s+([A-Za-z_]\w*)", "function"),
        (r"^\s*class\s+([A-Za-z_]\w*)", "class"),
        (r"^([A-Z][A-Z0-9_]{3,})\s*=", "config"),
    ],
    ".js": [
        (r"^\s*(?:async\s+)?function\s+([A-Za-z_]\w*)", "function"),
        (r"^\s*class\s+([A-Za-z_]\w*)", "class"),
        (r"^\s*(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\(", "function"),
    ],
    ".ts": [
        (r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)", "function"),
        (r"^\s*(?:export\s+)?class\s+([A-Za-z_]\w*)", "class"),
    ],
    ".java": [
        (r"^\s*(?:public|private|protected)?\s*(?:static\s+)?[\w<>\[\]]+\s+([A-Za-z_]\w*)\s*\(", "function"),
        (r"^\s*(?:public\s+)?class\s+([A-Za-z_]\w*)", "class"),
    ],
    ".kt": [
        (r"^\s*fun\s+([A-Za-z_]\w*)", "function"),
        (r"^\s*class\s+([A-Za-z_]\w*)", "class"),
    ],
    ".go": [
        (r"^\s*func\s+(?:\([^)]*\)\s+)?([A-Za-z_]\w*)", "function"),
    ],
    ".rs": [
        (r"^\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)", "function"),
        (r"^\s*(?:pub\s+)?struct\s+([A-Za-z_]\w*)", "class"),
    ],
    ".c": [
        (r"^[\w\s\*]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{?", "function"),
    ],
    ".h": [
        (r"^[\w\s\*]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*;", "function"),
    ],
    ".cpp": [
        (r"^[\w\s\*:<>,]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const\s*)?\{?", "function"),
        (r"^\s*class\s+([A-Za-z_]\w*)", "class"),
    ],
}

# Generic config-key pattern for added lines: KEY = value / key: value
CONFIG_KEY_RE = re.compile(r"^\s*([A-Za-z_][\w.\-]{2,})\s*[:=]")

FIXES_RE = re.compile(r"(?:fixes|closes|resolves)\s+#(\d+)", re.IGNORECASE)
REVERT_RE = re.compile(r'^[Rr]evert\s+"(.+)"')


@dataclass
class FileChange:
    path: str
    added: int
    removed: int
    status: str  # A / M / D / R


@dataclass
class SymbolChange:
    name: str
    kind: str  # function | class | config
    file: str
    change: str  # introduced | modified | removed


@dataclass
class CommitRecord:
    sha: str
    author: str
    email: str
    date: str  # ISO-8601
    parents: list
    subject: str
    body: str = ""
    files: list = field(default_factory=list)
    symbols: list = field(default_factory=list)
    fixes_issues: list = field(default_factory=list)
    is_revert: bool = False
    revert_of_subject: str | None = None
    revert_of_sha: str | None = None


def _git(repo_path: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr[:300]}")
    return proc.stdout


def ensure_clone(repo_url: str, dest: str) -> str:
    """Clone the repo if needed; return the working directory."""
    if os.path.isdir(os.path.join(dest, ".git")):
        return dest
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    print(f"cloning {repo_url} ...")
    subprocess.run(
        ["git", "clone", "--quiet", repo_url, dest],
        check=True,
    )
    return dest


def _parse_numstat_path(raw: str) -> tuple[str, str]:
    # Handles renames: "old => new" (possibly with braces).
    if "=>" in raw:
        new = raw.split("=>")[-1].strip().strip("}")
        return new, "R"
    return raw, "M"


def _metadata(repo_path: str, max_commits: int) -> dict[str, CommitRecord]:
    out = _git(
        repo_path,
        "log",
        f"--max-count={max_commits}",
        "--pretty=format:%H%x1f%P%x1f%aI%x1f%an%x1f%ae%x1f%s",
        "--numstat",
    )
    commits: dict[str, CommitRecord] = {}
    current: CommitRecord | None = None
    for line in out.split("\n"):
        if SHA_RE.match(line):
            f = line.split(FIELD_SEP)
            current = CommitRecord(
                sha=f[0],
                parents=f[1].split() if f[1] else [],
                date=f[2],
                author=f[3],
                email=f[4],
                subject=f[5] if len(f) > 5 else "",
            )
            commits[current.sha] = current
        elif current is not None and line.strip():
            parts = line.split("\t")
            if len(parts) == 3:
                added, removed, raw_path = parts
                path, status = _parse_numstat_path(raw_path)
                try:
                    current.files.append(
                        FileChange(
                            path=path,
                            added=int(added) if added != "-" else 0,
                            removed=int(removed) if removed != "-" else 0,
                            status=status,
                        )
                    )
                except ValueError:
                    continue
    return commits


def _bodies(repo_path: str, commits: dict[str, CommitRecord]) -> None:
    out = _git(
        repo_path,
        "log",
        f"--max-count={len(commits)}",
        "--pretty=format:%H%x1f%b%x1e",
    )
    for rec in out.split(REC_SEP):
        if FIELD_SEP not in rec:
            continue
        sha, body = rec.split(FIELD_SEP, 1)
        sha = sha.strip()
        if sha in commits:
            commits[sha].body = body.strip()


def _symbols_in_line(line: str, ext: str) -> list[tuple[str, str]]:
    found = []
    for pattern, kind in SYMBOL_PATTERNS.get(ext, []):
        m = re.match(pattern, line)
        if m:
            found.append((m.group(1), kind))
    if not found:
        m = CONFIG_KEY_RE.match(line)
        if m and m.group(1).isupper():
            found.append((m.group(1), "config"))
    return found


def _attach_symbols(
    repo_path: str, commits: dict[str, CommitRecord], max_commits: int
) -> None:
    out = _git(
        repo_path,
        "log",
        "--no-merges",
        f"--max-count={max_commits}",
        "--pretty=format:@@@%H@@@",
        "-p",
        "-U0",
    )
    sha: str | None = None
    path: str | None = None
    added: dict[str, set] = {}
    removed: dict[str, set] = {}

    def flush():
        # Flush ALL files seen for this commit (called at commit boundaries).
        if sha and sha in commits:
            rec = commits[sha]
            for p in added:
                a, r = added[p], removed.get(p, set())
                for name, kind in a - r:
                    rec.symbols.append(
                        SymbolChange(name, kind, p, "introduced")
                    )
                for name, kind in r - a:
                    rec.symbols.append(
                        SymbolChange(name, kind, p, "removed")
                    )
                for name, kind in a & r:
                    rec.symbols.append(
                        SymbolChange(name, kind, p, "modified")
                    )

    for line in out.split("\n"):
        if line.startswith("@@@") and line.endswith("@@@") and len(line) == 46:
            flush()
            sha, path = line[3:-3], None
            added, removed = {}, {}
        elif line.startswith("+++ b/"):
            path = line[6:]
            added.setdefault(path, set())
            removed.setdefault(path, set())
        elif path and sha and line.startswith("+") and not line.startswith("+++"):
            ext = os.path.splitext(path)[1].lower()
            for name, kind in _symbols_in_line(line[1:], ext):
                added[path].add((name, kind))
        elif path and sha and line.startswith("-") and not line.startswith("---"):
            ext = os.path.splitext(path)[1].lower()
            for name, kind in _symbols_in_line(line[1:], ext):
                removed[path].add((name, kind))
    flush()


def _trailers_and_reverts(commits: dict[str, CommitRecord]) -> None:
    by_subject = {c.subject: c.sha for c in commits.values()}
    for rec in commits.values():
        text = f"{rec.subject}\n{rec.body}"
        rec.fixes_issues = sorted(
            {int(n) for n in FIXES_RE.findall(text)}
        )
        m = REVERT_RE.match(rec.subject)
        if m:
            rec.is_revert = True
            rec.revert_of_subject = m.group(1)
            rec.revert_of_sha = by_subject.get(m.group(1))


def stream_commits(repo_path: str, max_commits: int) -> list[CommitRecord]:
    """Full ingest of git history into records. Ordered newest-first."""
    print(f"reading metadata for up to {max_commits} commits ...")
    commits = _metadata(repo_path, max_commits)
    print(f"  {len(commits)} commits")
    print("reading bodies ...")
    _bodies(repo_path, commits)
    print("reading diffs for symbol extraction ...")
    _attach_symbols(repo_path, commits, max_commits)
    _trailers_and_reverts(commits)
    n_sym = sum(len(c.symbols) for c in commits.values())
    print(f"  {n_sym} symbol changes extracted (heuristic)")
    return list(commits.values())
