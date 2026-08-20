"""Window-level tests for B4 — multi-page PDF page picker at import.

A multi-page PDF asks once (at import) which page to trace; that page is fixed
for the source (no in-session switching) and is stored on the source row so the
crop/metadata paths use the right page. A single-page PDF never prompts.
Skipped without PySide6 / PyMuPDF.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox
    from src.core.project_db import ProjectDB
    from src.ui.main_window import MainWindow
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False

try:
    import pymupdf as _fitz
    _HAVE_PYMUPDF = True
except Exception:  # pragma: no cover
    try:
        import fitz as _fitz
        _HAVE_PYMUPDF = True
    except Exception:
        _HAVE_PYMUPDF = False

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)


def _make_pdf(path, n_pages, sizes=None):
    doc = _fitz.open()
    for i in range(n_pages):
        w, h = (sizes[i] if sizes else (216.0, 144.0))
        doc.new_page(width=w, height=h)
    doc.save(str(path))
    doc.close()


@unittest.skipUnless(_HAVE_QT and _HAVE_PYMUPDF, "PySide6 / PyMuPDF not available")
class MultiPagePickerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.proj = ProjectDB.create(str(self.root / "proj"), name="Demo")
        self.win = MainWindow()
        self.win._set_project(self.proj)
        self._orig_getint = QInputDialog.getInt

    def tearDown(self):
        QInputDialog.getInt = self._orig_getint
        self.win.close()
        try:
            self.proj.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _stub_page(self, page1, ok=True):
        QInputDialog.getInt = staticmethod(lambda *a, **k: (page1, ok))

    def test_single_page_pdf_does_not_prompt(self):
        called = {"n": 0}

        def spy(*a, **k):
            called["n"] += 1
            return (1, True)
        QInputDialog.getInt = staticmethod(spy)
        pdf = self.root / "one.pdf"
        _make_pdf(pdf, 1)
        self.win.load_path(str(pdf))
        self.assertEqual(called["n"], 0)                 # never asked
        self.assertEqual(self.win._current_page, 0)
        # Stored page is None (single page), so nothing PDF-page-specific is recorded.
        src = self.proj.get_source(self.win._source_id)
        self.assertIsNone(src["page"])

    def test_multi_page_prompts_and_stores_chosen_page(self):
        # Distinct page sizes so the chosen page is identifiable by its render size.
        pdf = self.root / "multi.pdf"
        _make_pdf(pdf, 4, sizes=[(216, 144), (300, 200), (144, 216), (400, 100)])
        self._stub_page(3)                               # 1-based page 3 -> index 2
        self.win.load_path(str(pdf))
        self.assertEqual(self.win._current_page, 2)
        src = self.proj.get_source(self.win._source_id)
        self.assertEqual(src["page"], 2)
        # The rendered raster matches page 3's aspect (144 wide x 216 tall -> portrait).
        self.assertLess(self.win._raw_raster.width, self.win._raw_raster.height)

    def test_reopen_keeps_first_chosen_page_without_reprompting(self):
        pdf = self.root / "multi.pdf"
        _make_pdf(pdf, 3)
        self._stub_page(2)                               # choose page 2 first time
        self.win.load_path(str(pdf))
        self.assertEqual(self.win._current_page, 1)

        # Reopen: the picker must NOT be shown again; the stored page is reused.
        def boom(*a, **k):
            raise AssertionError("should not re-prompt for an already-imported source")
        QInputDialog.getInt = staticmethod(boom)
        self.win.load_path(str(pdf))
        self.assertEqual(self.win._current_page, 1)

    def test_cancel_defaults_to_first_page(self):
        pdf = self.root / "multi.pdf"
        _make_pdf(pdf, 3)
        self._stub_page(2, ok=False)                     # user cancels the dialog
        self.win.load_path(str(pdf))
        self.assertEqual(self.win._current_page, 0)


if __name__ == "__main__":
    unittest.main()
