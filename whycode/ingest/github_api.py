"""Pulls pull requests and issues from the GitHub API.

Everything is capped (max PRs/issues, comments per item) and it backs off
when rate-limited. Works without a token at 60 req/hr — enough for a
small demo ingest, not much more.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

API = "https://api.github.com"


@dataclass
class PullRequestRecord:
    number: int
    title: str
    body: str
    user: str
    created_at: str
    merged_at: str | None
    merge_commit_sha: str | None
    url: str
    comments: list = field(default_factory=list)  # [{user, created_at, body}]


@dataclass
class IssueRecord:
    number: int
    title: str
    body: str
    user: str
    created_at: str
    closed_at: str | None
    url: str
    comments: list = field(default_factory=list)


class GitHubClient:
    def __init__(self, token: str | None = None):
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/vnd.github+json"
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _get(self, url: str, params: dict | None = None):
        while True:
            resp = self.session.get(url, params=params, timeout=30)
            if resp.status_code == 403 and resp.headers.get(
                "X-RateLimit-Remaining"
            ) == "0":
                reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset - time.time(), 0) + 5
                print(f"  rate limited, sleeping {int(wait)}s ...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()

    def _comments(self, owner: str, repo: str, number: int, limit: int = 20):
        try:
            data = self._get(
                f"{API}/repos/{owner}/{repo}/issues/{number}/comments",
                {"per_page": limit},
            )
        except requests.HTTPError:
            return []
        return [
            {
                "user": c.get("user", {}).get("login", "?"),
                "created_at": c.get("created_at", ""),
                "body": (c.get("body") or "")[:2000],
            }
            for c in data[:limit]
        ]

    def iter_prs(self, owner: str, repo: str, max_prs: int):
        page, seen = 1, 0
        while seen < max_prs:
            batch = self._get(
                f"{API}/repos/{owner}/{repo}/pulls",
                {
                    "state": "all",
                    "per_page": 100,
                    "page": page,
                    "sort": "created",
                    "direction": "desc",
                },
            )
            if not batch:
                break
            for pr in batch:
                if seen >= max_prs:
                    return
                seen += 1
                yield PullRequestRecord(
                    number=pr["number"],
                    title=pr.get("title", ""),
                    body=(pr.get("body") or "")[:8000],
                    user=pr.get("user", {}).get("login", "?"),
                    created_at=pr.get("created_at", ""),
                    merged_at=pr.get("merged_at"),
                    merge_commit_sha=pr.get("merge_commit_sha"),
                    url=pr.get("html_url", ""),
                    comments=self._comments(owner, repo, pr["number"]),
                )
            page += 1

    def iter_issues(self, owner: str, repo: str, max_issues: int):
        page, seen = 1, 0
        while seen < max_issues:
            batch = self._get(
                f"{API}/repos/{owner}/{repo}/issues",
                {
                    "state": "all",
                    "per_page": 100,
                    "page": page,
                    "sort": "created",
                    "direction": "desc",
                },
            )
            if not batch:
                break
            for issue in batch:
                if "pull_request" in issue:  # PRs also appear here; skip them
                    continue
                if seen >= max_issues:
                    return
                seen += 1
                yield IssueRecord(
                    number=issue["number"],
                    title=issue.get("title", ""),
                    body=(issue.get("body") or "")[:8000],
                    user=issue.get("user", {}).get("login", "?"),
                    created_at=issue.get("created_at", ""),
                    closed_at=issue.get("closed_at"),
                    url=issue.get("html_url", ""),
                    comments=self._comments(owner, repo, issue["number"]),
                )
            page += 1
