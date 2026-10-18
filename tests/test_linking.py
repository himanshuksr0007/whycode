"""Unit tests for question -> entity candidate extraction (no Neo4j needed)."""
import unittest

from whycode.retrieval.linking import extract_candidates


class TestLinking(unittest.TestCase):
    def test_path_extraction(self):
        c = extract_candidates("why is src/app.py doing retries?")
        self.assertIn("src/app.py", c["paths"])

    def test_backticked_symbol(self):
        c = extract_candidates("why does `MAX_RETRIES` exist?")
        self.assertIn("MAX_RETRIES", c["symbols"])

    def test_camel_and_snake(self):
        c = extract_candidates("why does RetryPolicy use back_off?")
        syms = c["symbols"]
        self.assertIn("RetryPolicy", syms)
        self.assertIn("back_off", syms)

    def test_stopwords_excluded(self):
        c = extract_candidates("why does this code do that thing")
        for s in c["symbols"]:
            self.assertNotIn(s.lower(), {"why", "this", "that"})

    def test_keywords_present(self):
        c = extract_candidates("why was the reloader changed?")
        self.assertIn("reloader", c["keywords"])


if __name__ == "__main__":
    unittest.main()
