"""Relevance scoring: rationale + discussion beats drive-by PRs."""
import unittest
from types import SimpleNamespace

from whycode.ingest.relevance import is_high_signal, score_pr


def pr(title="", body="", comments=0, review_comments=0, files=0):
    return SimpleNamespace(
        title=title, body=body,
        comments=[{}] * comments,
        review_comments=[{}] * review_comments,
        files=[{}] * files,
    )


class TestRelevance(unittest.TestCase):
    def test_bare_pr_scores_low(self):
        self.assertLess(score_pr(pr(title="typo", body="fix typo")), 0.5)

    def test_rationale_and_discussion_scores_high(self):
        p = pr(title="Change retry defaults because of thundering herd",
               body="We decided to disable retries instead of the old policy",
               comments=6, review_comments=4, files=3)
        s = score_pr(p)
        self.assertGreaterEqual(s, 0.5)
        self.assertTrue(is_high_signal(s))

    def test_score_is_bounded(self):
        p = pr(title="because why rationale decision instead of trade-offs",
               body="fixes #1 closes #2", comments=100,
               review_comments=100, files=10)
        self.assertLessEqual(score_pr(p), 1.0)
        self.assertGreaterEqual(score_pr(p), 0.0)

    def test_deterministic(self):
        p = pr(title="why we did it because reasons", comments=3)
        self.assertEqual(score_pr(p), score_pr(p))


if __name__ == "__main__":
    unittest.main()
