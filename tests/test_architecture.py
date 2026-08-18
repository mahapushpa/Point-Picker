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


if __name__ == "__main__":
    sys.exit(unittest.main())
