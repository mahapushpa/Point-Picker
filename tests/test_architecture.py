"""Architecture guard: src/core and src/io must import zero UI frameworks.

This enforces the non-negotiable rule in PROJECT_BRIEF.md - all real logic stays
pure Python so a Phase 2 UI rewrite is UI-only. A leak here is a defect to fix
immediately, so it is a failing test, not a lint suggestion.
"""

import ast
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

FORBIDDEN_ROOTS = {
    "PySide6", "PySide2", "PyQt5", "PyQt6", "PyQt", "shiboken6",
    "tkinter", "wx", "kivy", "gi",
}
PURE_PACKAGES = ("core", "io", "export")


def _imported_roots(pyfile: Path):
    tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                roots.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _duplicate_defs(tree):
    """Yield (scope, name, line) for every function/method defined more than once
    within the same scope (module body or a class body). A later ``def`` silently
    replaces an earlier one in Python, so a duplicate is almost always a bug — e.g.
    two ``ProjectDB.list_unit_profiles`` where the second reverted an ordering
    guarantee (M9 regression, caught here going forward)."""
    def scan(scope_name, body):
        seen = {}
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in seen:
                    yield (scope_name, node.name, node.lineno)
                seen[node.name] = node.lineno
            if isinstance(node, ast.ClassDef):
                yield from scan(f"{scope_name}.{node.name}", node.body)
    yield from scan("<module>", tree.body)


class NoUiImportsInPureLayers(unittest.TestCase):
    def test_pure_packages_have_no_ui_imports(self):
        checked = 0
        for pkg in PURE_PACKAGES:
            for pyfile in (SRC / pkg).rglob("*.py"):
                checked += 1
                leaked = _imported_roots(pyfile) & FORBIDDEN_ROOTS
                self.assertFalse(
                    leaked,
                    f"{pyfile.relative_to(SRC)} imports UI framework(s): {sorted(leaked)}",
                )
        self.assertGreater(checked, 0, "no pure-layer modules were scanned")


class NoDuplicateDefinitions(unittest.TestCase):
    def test_no_duplicate_function_or_method_names(self):
        checked = 0
        for pyfile in SRC.rglob("*.py"):
            checked += 1
            tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
            dups = list(_duplicate_defs(tree))
            self.assertFalse(
                dups,
                f"{pyfile.relative_to(SRC)} has duplicate definition(s) (a later "
                f"def silently shadows the earlier one): "
                + "; ".join(f"{scope}.{name} at line {line}" for scope, name, line in dups),
            )
        self.assertGreater(checked, 0, "no modules were scanned")


if __name__ == "__main__":
    sys.exit(unittest.main())
