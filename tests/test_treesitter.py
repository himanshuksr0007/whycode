"""Tree-sitter extraction: precise symbols when available, None when not."""
import unittest

from whycode.ingest import treesitter_extract as tse

try:
    import tree_sitter  # noqa: F401
    import tree_sitter_python  # noqa: F401
    HAVE_TS = tse._language() is not None
except Exception:
    HAVE_TS = False

SAMPLE = '''
import os
from sys import path as spath

TIMEOUT = 30

class Client:
    def get(self, url):
        def _inner():
            pass
        return _inner()

def top_level():
    pass
'''


class TestTreeSitter(unittest.TestCase):
    def test_non_python_returns_none(self):
        self.assertIsNone(tse.extract("x.js", "function f() {}"))

    @unittest.skipUnless(HAVE_TS, "tree-sitter not installed")
    def test_extracts_functions_classes_imports_config(self):
        syms = tse.extract("x.py", SAMPLE)
        by_name = {n: k for n, k, _, _ in syms}
        self.assertEqual(by_name.get("Client"), "class")
        self.assertEqual(by_name.get("get"), "function")
        self.assertEqual(by_name.get("top_level"), "function")
        self.assertEqual(by_name.get("TIMEOUT"), "config")
        self.assertEqual(by_name.get("os"), "import")
        # nested function found, with sane line bounds
        inner = [s for s in syms if s[0] == "_inner"][0]
        self.assertLess(inner[2], inner[3])

    @unittest.skipUnless(HAVE_TS, "tree-sitter not installed")
    def test_def_in_string_is_not_a_symbol(self):
        syms = tse.extract("x.py", 'DOC = """\ndef fake():\n"""\n')
        names = {n for n, _, _, _ in syms}
        self.assertNotIn("fake", names)


if __name__ == "__main__":
    unittest.main()
