"""Stable evidence IDs: same input, same ID, every run."""
import unittest

from whycode.evidence_ids import (commit_id, decision_id_for_commit,
                                  decision_id_for_pr, issue_comment_id,
                                  issue_id, observation_id, pr_id, repo_key,
                                  review_comment_id)


class TestEvidenceIds(unittest.TestCase):
    def test_repo_key(self):
        self.assertEqual(repo_key("encode/httpx"), "encode:httpx")

    def test_ids_are_stable_and_namespaced(self):
        rk = repo_key("encode/httpx")
        self.assertEqual(pr_id(rk, 123), "encode:httpx:pr:123")
        self.assertEqual(issue_id(rk, 7), "encode:httpx:issue:7")
        self.assertEqual(commit_id(rk, "abc"), "encode:httpx:commit:abc")
        self.assertEqual(review_comment_id(rk, 9),
                         "encode:httpx:review_comment:9")
        self.assertEqual(issue_comment_id(rk, 9),
                         "encode:httpx:issue_comment:9")
        self.assertEqual(decision_id_for_commit(rk, "abc"),
                         "encode:httpx:decision:abc")
        self.assertEqual(decision_id_for_pr(rk, 123),
                         "encode:httpx:decision:pr-123")
        self.assertEqual(observation_id(rk, "x.py"),
                         "encode:httpx:observation:x.py")

    def test_pr_and_issue_ids_dont_collide(self):
        rk = repo_key("encode/httpx")
        self.assertNotEqual(pr_id(rk, 1), issue_id(rk, 1))


if __name__ == "__main__":
    unittest.main()
