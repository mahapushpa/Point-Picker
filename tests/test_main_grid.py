"""Window-level tests for M19 grid-detection scale in the UI: image-only, offered
not auto-applied, honest when no grid is found, and cross-checked against a manual
scale. Skipped without PySide6 / Pillow.
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
    from PySide6.QtWidgets import QApplication, QMessageBox, QInputDialog
    from src.ui.main_window import MainWindow
    from src.core.scale import compute_two_point_scale, METHOD_GRID
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False

try:
    import numpy as np
    from PIL import Image
    _HAVE_IMG = True
except Exception:  # pragma: no cover
    _HAVE_IMG = False

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)


def _answer(button):
    QMessageBox.question = staticmethod(lambda *a, **k: button)


def _interval(value, ok=True):
    QInputDialog.getDouble = staticmethod(lambda *a, **k: (value, ok))


def _grid_png(path, spacing=20, size=200):
    g = np.full((size, size), 255, dtype=np.uint8)
    for x in range(spacing, size - 1, spacing):
        g[:, x] = 0
    for y in range(spacing, size - 1, spacing):
        g[y, :] = 0
    Image.fromarray(g, mode="L").convert("RGB").save(path)


def _blank_png(path, size=200):
    Image.new("RGB", (size, size), (255, 255, 255)).save(path)


@unittest.skipUnless(_HAVE_QT and _HAVE_IMG, "PySide6 / Pillow / numpy not available")
class GridScaleUiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.win = MainWindow()

    def tearDown(self):
        self.win.close()
        self._tmp.cleanup()

    def _load(self, maker):
        p = Path(self._tmp.name) / "sheet.png"
        maker(p)
        self.win.load_path(str(p))

    def test_action_enabled_for_image(self):
        self._load(_grid_png)
        self.assertTrue(self.win._grid_scale_action.isEnabled())

    def test_no_grid_is_honest_and_applies_nothing(self):
        self._load(_blank_png)
        _answer(QMessageBox.StandardButton.Yes)   # even if the user would say yes...
        _interval(10.0)
        self.win.propose_grid_scale()              # ...no grid -> nothing applied
        self.assertIsNone(self.win._scale)

    def test_offered_not_auto_applied_when_declined(self):
        self._load(_grid_png)
        _interval(10.0)
        _answer(QMessageBox.StandardButton.No)
        self.win.propose_grid_scale()
        self.assertIsNone(self.win._scale)

    def test_applied_on_accept_with_correct_value(self):
        self._load(_grid_png)          # 20 px per cell
        _interval(10.0)                # 10 m per cell -> 0.5 m/px
        _answer(QMessageBox.StandardButton.Yes)
        self.win.propose_grid_scale()
        self.assertIsNotNone(self.win._scale)
        self.assertEqual(self.win._scale.method, METHOD_GRID)
        self.assertAlmostEqual(self.win._scale.metres_per_pixel, 0.5, delta=0.03)

    def test_cross_check_against_manual_keeps_on_decline(self):
        self._load(_grid_png)
        self.win._scale = compute_two_point_scale((0, 0), (100, 0), 50.0)  # 0.5 m/px
        _interval(10.0)
        _answer(QMessageBox.StandardButton.No)
        self.win.propose_grid_scale()
        self.assertEqual(self.win._scale.method, "two-point")   # manual kept

    def test_cancel_interval_applies_nothing(self):
        self._load(_grid_png)
        _interval(0.0, ok=False)       # user cancels the interval prompt
        self.win.propose_grid_scale()
        self.assertIsNone(self.win._scale)


@unittest.skipUnless(_HAVE_QT and _HAVE_IMG, "PySide6 / Pillow / numpy not available")
class GridScaleNonImageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.win = MainWindow()

    def tearDown(self):
        self.win.close()
        self._tmp.cleanup()

    def test_disabled_and_noop_for_pdf(self):
        try:
            import pymupdf as fitz
        except Exception:
            try:
                import fitz
            except Exception:
                self.skipTest("PyMuPDF not available")
        p = Path(self._tmp.name) / "doc.pdf"
        doc = fitz.open()
        doc.new_page(width=200, height=200)
        doc.save(str(p))
        doc.close()
        self.win.load_path(str(p))
        self.assertFalse(self.win._grid_scale_action.isEnabled())
        _answer(QMessageBox.StandardButton.Yes)
        self.win.propose_grid_scale()          # image-only guard
        self.assertIsNone(self.win._scale)


if __name__ == "__main__":
    unittest.main()
