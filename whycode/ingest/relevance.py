"""Scores PRs for how likely they are to contain a real decision.

Not every PR is worth extracting decisions from — typo fixes and dep bumps
are noise. This is a cheap heuristic to pick the high-signal ones first.
Tune the weights per repo; the shape matters more than the numbers.
"""
from __future__ import annotations

from .intent import _has_rationale  # reuse the rationale keywords


def score_pr(pr) -> float:
    """0.0 - 1.0. pr needs: title, body, comments, review_comments."""
    score = 0.0
    text = f"{pr.title}\n{pr.body}"
    if _has_rationale(text):
        score += 0.35
    n_comments = len(getattr(pr, "comments", []) or [])
    score += min(n_comments / 10.0, 1.0) * 0.20
    n_review = len(getattr(pr, "review_comments", []) or [])
    score += min(n_review / 8.0, 1.0) * 0.20
    if "fix" in text.lower() or "close" in text.lower():
        score += 0.10  # linked issue/PR discussion
    n_files = len(getattr(pr, "files", []) or [])
    if 1 < n_files <= 20:
        score += 0.10  # meatier than a typo, smaller than a rewrite
    return round(min(score, 1.0), 2)


def is_high_signal(score: float, threshold: float = 0.5) -> bool:
    return score >= threshold
