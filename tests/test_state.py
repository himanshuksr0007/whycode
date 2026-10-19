"""Ingest state: resume skips completed items, failures are recorded."""
import os
import tempfile
import unittest

from whycode.ingest.state import IngestState


class TestIngestState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = IngestState(os.path.join(self.tmp, "s.sqlite"))

    def tearDown(self):
        self.st.close()

    def test_complete_roundtrip(self):
        self.assertFalse(self.st.is_complete("encode/httpx", "pr", 1))
        self.st.mark_complete("encode/httpx", "pr", 1)
        self.assertTrue(self.st.is_complete("encode/httpx", "pr", 1))
        self.assertFalse(self.st.is_complete("encode/httpx", "pr", 2))

    def test_completed_ids(self):
        self.st.mark_complete("encode/httpx", "pr", 3)
        self.st.mark_complete("encode/httpx", "pr", 5)
        self.st.mark_complete("encode/httpx", "issue", 5)
        self.assertEqual(self.st.completed_ids("encode/httpx", "pr"), {3, 5})
        self.assertEqual(self.st.completed_ids("encode/httpx", "issue"), {5})

    def test_failed_does_not_count_as_complete(self):
        self.st.mark_failed("encode/httpx", "pr", 9, "boom")
        self.assertFalse(self.st.is_complete("encode/httpx", "pr", 9))
        self.assertEqual(self.st.completed_ids("encode/httpx", "pr"), set())

    def test_raw_payload_roundtrip(self):
        self.st.save_raw("encode:httpx:pr:1", "GET /x", {"a": 1})
        self.st.commit()
        row = self.st.conn.execute(
            "SELECT payload FROM raw_payloads WHERE source_id=?",
            ("encode:httpx:pr:1",)).fetchone()
        self.assertIn('"a": 1', row[0])


if __name__ == "__main__":
    unittest.main()
