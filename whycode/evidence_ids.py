"""Stable, content-addressed evidence IDs.

Every thing we store gets an ID you can reconstruct from the thing itself:
`{repo_key}:{type}:{natural key}`. No UUIDs, no counters — reruns produce
the same IDs, which is what makes ingestion idempotent.
"""
from __future__ import annotations


def repo_key(repo: str) -> str:
    """'encode/httpx' -> 'encode:httpx'."""
    return repo.replace("/", ":")


def commit_id(rk: str, sha: str) -> str:
    return f"{rk}:commit:{sha}"


def pr_id(rk: str, number: int) -> str:
    return f"{rk}:pr:{number}"


def issue_id(rk: str, number: int) -> str:
    return f"{rk}:issue:{number}"


def review_comment_id(rk: str, comment_id: int) -> str:
    return f"{rk}:review_comment:{comment_id}"


def issue_comment_id(rk: str, comment_id: int) -> str:
    return f"{rk}:issue_comment:{comment_id}"


def decision_id_for_commit(rk: str, sha: str) -> str:
    return f"{rk}:decision:{sha}"


def decision_id_for_pr(rk: str, number: int) -> str:
    return f"{rk}:decision:pr-{number}"


def observation_id(rk: str, path: str) -> str:
    return f"{rk}:observation:{path}"
