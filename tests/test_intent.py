"""Unit tests for the intent layer, incl. the anti-hallucination gate."""

RK = "test:repo"
import unittest
from types import SimpleNamespace

from whycode.ingest.intent import (
    classify_commit,
    derive_observations,
    supersession_edges,
    Decision,
)


def rec(**kw):
    base = dict(
        sha="a" * 40, author="dev", email="d@x.io", date="2024-01-01",
        parents=[], subject="do a thing", body="",
        files=[], symbols=[], fixes_issues=[],
        is_revert=False, revert_of_subject=None, revert_of_sha=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestGate(unittest.TestCase):
    def test_plain_commit_yields_no_decision(self):
        # THE gate: a commit with no rationale signals must not become a Decision.
        r = rec(subject="fix typo", body="oops")
        self.assertIsNone(classify_commit(r, RK))

    def test_every_decision_has_evidence(self):
        cases = [
            rec(subject='Revert "old thing"', is_revert=True,
                revert_of_sha="b" * 40),
            rec(subject="fix crash", fixes_issues=[12]),
            rec(subject="choose X because Y is slow", body="rationale here"),
        ]
        for r in cases:
            d = classify_commit(r, RK)
            self.assertIsNotNone(d)
            self.assertTrue(d.evidence, f"{d.kind} has no evidence")

    def test_revert_kind_and_target(self):
        r = rec(subject='Revert "old thing"', is_revert=True,
                revert_of_sha="b" * 40)
        d = classify_commit(r, RK)
        self.assertEqual(d.kind, "revert")
        self.assertEqual(d.supersedes_sha, "b" * 40)

    def test_doc_commit(self):
        f = SimpleNamespace(path="docs/adr/0001-x.md", added=10,
                            removed=0, status="A")
        r = rec(subject="document the retry policy", files=[f])
        d = classify_commit(r, RK)
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "doc")


class TestObservations(unittest.TestCase):
    def test_hotspot_without_rationale(self):
        stats = {"a.py": {"commits": 12, "decisions": 0},
                 "b.py": {"commits": 12, "decisions": 3}}
        obs = derive_observations(stats, RK)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].about_files, ["a.py"])
        self.assertEqual(obs[0].kind, "unexplained_hotspot")


class TestSupersession(unittest.TestCase):
    def _d(self, id, summary, date, files=("x.py",)):
        return Decision(id=id, kind="rationale", summary=summary, date=date,
                        evidence=["commit:1"], confidence=0.7,
                        about_files=list(files))

    def test_explicit_revert_edge(self):
        old = self._d("test:repo:decision:1", "use threads", "2023-01-01")
        new = self._d("test:repo:decision:2", 'Revert "use threads"',
                      "2024-01-01")
        new.supersedes_sha = "1"
        edges = supersession_edges([old, new], rk=RK)
        self.assertTrue(
            any(e["newer"] == "test:repo:decision:2"
                and e["older"] == "test:repo:decision:1"
                and e["method"] == "explicit" for e in edges))

    def test_heuristic_edge_on_overlap(self):
        old = self._d("d:1", "retry three times because upstream flakes",
                      "2023-01-01")
        new = self._d("d:2", "retry five times because upstream still flakes",
                      "2024-01-01")
        edges = supersession_edges([old, new])
        heur = [e for e in edges if e["method"] == "heuristic"]
        self.assertTrue(heur)

    def test_no_edge_without_overlap(self):
        old = self._d("d:1", "rename the widget factory", "2023-01-01")
        new = self._d("d:2", "bump the docker base image", "2024-01-01")
        edges = supersession_edges([old, new])
        self.assertFalse(
            [e for e in edges
             if {e["newer"], e["older"]} == {"d:1", "d:2"}])

    def test_tie_dates_use_explicit_order(self):
        # Same-second commits: without `order` the direction can flip.
        old = self._d("d:1", "retry three times because upstream flakes",
                      "2024-01-01")
        new = self._d("d:2", "retry five times because upstream still flakes",
                      "2024-01-01")
        edges = supersession_edges([new, old],  # newest-first input
                                   order={"d:1": 0, "d:2": 1})
        heur = [e for e in edges if e["method"] == "heuristic"]
        self.assertEqual(len(heur), 1)
        self.assertEqual(heur[0]["newer"], "d:2")
        self.assertEqual(heur[0]["older"], "d:1")


if __name__ == "__main__":
    unittest.main()
