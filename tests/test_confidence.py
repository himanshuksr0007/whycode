"""Unit tests for confidence scoring and refusal behavior."""
import unittest

from whycode.retrieval.qa import score_confidence, WEIGHTS


class TestConfidence(unittest.TestCase):
    def test_full_evidence_scores_high(self):
        score, breakdown = score_confidence({k: True for k in WEIGHTS})
        self.assertAlmostEqual(score, 1.0)
        self.assertTrue(all(v > 0 for v in breakdown.values()))

    def test_decision_alone_is_below_default_threshold(self):
        # A lone decision (0.30) must NOT clear the 0.45 threshold: this is
        # what makes refusal the default for thin evidence.
        score, _ = score_confidence({"has_decision": True})
        self.assertLess(score, 0.45)

    def test_decision_plus_discussion_clears_threshold(self):
        score, _ = score_confidence(
            {"has_decision": True, "discussion": True})
        self.assertGreaterEqual(score, 0.45)

    def test_empty_flags_score_zero(self):
        score, _ = score_confidence({})
        self.assertEqual(score, 0.0)

    def test_breakdown_sums_to_score(self):
        score, breakdown = score_confidence(
            {"has_decision": True, "person": True, "recent": True})
        self.assertAlmostEqual(score, sum(breakdown.values()))


if __name__ == "__main__":
    unittest.main()
