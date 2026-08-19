"""Window-level tests for M14 PDF-metadata scale in the UI.

Verify the review-critical contract: a metadata scale is *offered*, never applied
silently, and when a manual scale already exists both are shown and cross-checked
rather than one being picked. Uses a real generated PDF; skipped without
PySide6 / PyMuPDF.
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
    from PySide6.QtWidgets import QApplication, QMessageBox
    from src.core.project_db import ProjectDB
    from src.core.scale import compute_two_point_scale, METHOD_PDF_METADATA
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


def _answer(button):
    QMessageBox.question = staticmethod(lambda *a, **k: button)


@unittest.skipUnless(_HAVE_QT and _HAVE_PYMUPDF, "PySide6 / PyMuPDF not available")
class MetadataScaleUiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.proj = ProjectDB.create(str(root / "proj"), name="Demo")
        self.pdf = root / "plan.pdf"
        doc = _fitz.open()
        doc.new_page(width=216.0, height=144.0)   # 3in x 2in
        doc.save(str(self.pdf))
        doc.close()

        self.win = MainWindow()
        self.win._set_project(self.proj)
        self.win.load_path(str(self.pdf))   # renders + attaches the source
        self.sid = self.win._source_id
        self.assertIsNotNone(self.sid)

    def tearDown(self):
        self.win.close()
        try:
            self.proj.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_action_enabled_for_pdf(self):
        self.assertTrue(self.win._pdf_scale_action.isEnabled())

    def test_offered_not_auto_applied_when_declined(self):
        self.assertIsNone(self.proj.get_source_scale(self.sid))
        _answer(QMessageBox.StandardButton.No)          # user declines the offer
        self.win.propose_pdf_metadata_scale()
        self.assertIsNone(self.win._scale)               # nothing applied
        self.assertIsNone(self.proj.get_source_scale(self.sid))

    def test_applied_only_on_explicit_accept(self):
        _answer(QMessageBox.StandardButton.Yes)
        self.win.propose_pdf_metadata_scale()
        row = self.proj.get_source_scale(self.sid)
        self.assertIsNotNone(row)
        self.assertEqual(row["method"], METHOD_PDF_METADATA)
        # 216 pt = 3 in rendered at 150 dpi -> 0.0254/150 m per pixel.
        self.assertAlmostEqual(row["metres_per_pixel"], 0.0254 / 150, places=6)
        self.assertEqual(self.win._scale.method, METHOD_PDF_METADATA)

    def test_cross_check_keeps_manual_when_declined(self):
        # A manual scale exists first; the metadata offer must not silently replace
        # it, and declining the cross-check keeps the manual scale intact.
        self.win._scale = compute_two_point_scale((0, 0), (100, 0), 50.0)  # 0.5 m/px
        self.win._persist_scale()
        _answer(QMessageBox.StandardButton.No)
        self.win.propose_pdf_metadata_scale()
        row = self.proj.get_source_scale(self.sid)
        self.assertEqual(row["method"], "two-point")
        self.assertAlmostEqual(self.win._scale.metres_per_pixel, 0.5)

    def test_cross_check_replaces_manual_on_accept(self):
        self.win._scale = compute_two_point_scale((0, 0), (100, 0), 50.0)
        self.win._persist_scale()
        _answer(QMessageBox.StandardButton.Yes)
        self.win.propose_pdf_metadata_scale()
        row = self.proj.get_source_scale(self.sid)
        self.assertEqual(row["method"], METHOD_PDF_METADATA)
        self.assertAlmostEqual(row["metres_per_pixel"], 0.0254 / 150, places=6)


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class NonPdfTests(unittest.TestCase):
    def setUp(self):
        try:
            from PIL import Image
        except Exception:
            self.skipTest("Pillow not available")
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.proj = ProjectDB.create(str(root / "proj"), name="Demo")
        self.png = root / "sheet.png"
        Image.new("RGB", (200, 200), (255, 255, 255)).save(self.png)
        self.win = MainWindow()
        self.win._set_project(self.proj)
        self.win.load_path(str(self.png))

    def tearDown(self):
        self.win.close()
        try:
            self.proj.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_action_disabled_and_noop_for_images(self):
        self.assertFalse(self.win._pdf_scale_action.isEnabled())
        _answer(QMessageBox.StandardButton.Yes)     # even if a dialog said yes...
        self.win.propose_pdf_metadata_scale()        # ...an image has no metadata path
        self.assertIsNone(self.win._scale)
        self.assertIsNone(self.proj.get_source_scale(self.win._source_id))


if __name__ == "__main__":
    unittest.main()
