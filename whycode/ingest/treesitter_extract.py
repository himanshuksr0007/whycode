"""Symbol extraction with tree-sitter (Python for now).

Returns a list of (name, kind, start_line, end_line) or None when
tree-sitter isn't installed / the file isn't Python. Callers fall back
to the regex heuristics in that case — never crash the ingest because
an optional parser is missing.
"""
from __future__ import annotations

_LANG = None
_Tried = False


def _language():
    global _LANG, _Tried
    if _Tried:
        return _LANG
    _Tried = True
    try:
        from tree_sitter import Language
        import tree_sitter_python
        _LANG = Language(tree_sitter_python.language())
    except Exception:
        _LANG = None
    return _LANG


def extract(path: str, text: str):
    """Parse one file. Kinds: function | class | import | config."""
    lang = _language() if path.endswith(".py") else None
    if lang is None:
        return None
    try:
        from tree_sitter import Parser
        parser = Parser(lang)
        tree = parser.parse(text.encode("utf-8", errors="replace"))
    except Exception:
        return None

    root = tree.root_node
    out = []

    def name_of(node):
        for child in node.children:
            if child.type == "identifier":
                return text[child.start_byte:child.end_byte]
        return None

    def walk(node, depth=0):
        for child in node.children:
            if child.type == "function_definition":
                n = name_of(child)
                if n:
                    out.append((n, "function",
                                child.start_point[0] + 1,
                                child.end_point[0] + 1))
            elif child.type == "class_definition":
                n = name_of(child)
                if n:
                    out.append((n, "class",
                                child.start_point[0] + 1,
                                child.end_point[0] + 1))
            elif child.type in ("import_statement", "import_from_statement"):
                # first dotted name in the statement
                mod = text[child.start_byte:child.end_byte].split()
                target = mod[1] if len(mod) > 1 else ""
                target = target.strip("()").split(",")[0].strip()
                if target and target != ".":
                    out.append((target.split(".")[0], "import",
                                child.start_point[0] + 1,
                                child.end_point[0] + 1))
            elif (child.type == "expression_statement" and depth == 0
                    and child.children
                    and child.children[0].type == "assignment"):
                lhs = child.children[0].child_by_field_name("left")
                if (lhs is not None and lhs.type == "identifier"):
                    n = text[lhs.start_byte:lhs.end_byte]
                    if n.isupper() and len(n) > 2:
                        out.append((n, "config",
                                    child.start_point[0] + 1,
                                    child.end_point[0] + 1))
            walk(child, depth + 1)

    walk(root)
    # de-dupe, keep first occurrence
    seen, deduped = set(), []
    for item in out:
        if (item[0], item[1]) not in seen:
            seen.add((item[0], item[1]))
            deduped.append(item)
    return deduped
