"""Window-level tests for M17 point-data guard rails.

Both checks must surface as dismissible warnings and never block, remove, or move
a placed point. Skipped without PySide6 / Pillow.
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
    from PySide6.QtCore import QPointF
    from src.ui.main_window import MainWindow
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False

try:
    import numpy as np
    from PIL import Image
    _HAVE_IMG = True
except Exception:  # pragma: no cover
    _HAVE_IMG = False

try:
    import ezdxf
    _HAVE_EZDXF = True
except Exception:  # pragma: no cover
    _HAVE_EZDXF = False

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)


def _write_L_png(path):
    gray = np.full((200, 200), 255, dtype=np.uint8)
    gray[30:151, 27:34] = 0
    gray[147:154, 30:151] = 0
    Image.fromarray(gray, mode="L").convert("RGB").save(path)


@unittest.skipUnless(_HAVE_QT and _HAVE_IMG, "PySide6 / Pillow / numpy not available")
class GuardRailUiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.png = Path(self._tmp.name) / "sheet.png"
        _write_L_png(self.png)
        self.win = MainWindow()
        self.win.load_path(str(self.png))

    def tearDown(self):
        self.win.close()
        self._tmp.cleanup()

    # -- duplicate point (check 1) -----------------------------------------

    def test_duplicate_tracing_point_warns_but_is_kept(self):
        self.win.canvas.start_polygon()
        self.win.canvas._place_point(QPointF(100, 100))
        self.win.canvas._place_point(QPointF(103, 102))   # ~3.6 px: a double-click
        # Warned...
        self.assertIn("double-click", self.win._status.text())
        # ...but BOTH points kept — never auto-removed / blocked.
        self.assertEqual(len(self.win.canvas.polygon_points()), 2)

    def test_distinct_close_point_does_not_warn(self):
        self.win.canvas.start_polygon()
        self.win.canvas._place_point(QPointF(100, 100))
        self.win._status.setText("clean")
        self.win.canvas._place_point(QPointF(130, 100))   # 30 px: distinct
        self.assertNotIn("double-click", self.win._status.text())
        self.assertEqual(len(self.win.canvas.polygon_points()), 2)

    # -- missing corner (check 2) ------------------------------------------

    def test_cut_corner_flags_edge_without_changing_points(self):
        cut = [(30, 30), (150, 150)]
        self.win.canvas.set_polygon(cut, closed=False)
        self.win.check_missing_corners(announce=False)
        self.assertEqual(self.win.canvas.edge_warnings(), {0})
        self.assertIn("missed", self.win._status.text().lower())
        # No auto-correction: the traced points are exactly as they were.
        self.assertEqual(self.win.canvas.polygon_points(), cut)

    def test_clean_trace_reports_no_warnings(self):
        clean = [(30, 30), (30, 150), (150, 150)]
        self.win.canvas.set_polygon(clean, closed=False)
        self.win.check_missing_corners(announce=False)
        self.assertEqual(self.win.canvas.edge_warnings(), set())
        self.assertIn("no suspicious", self.win._status.text().lower())

    def test_editing_clears_stale_warnings(self):
        self.win.canvas.set_polygon([(30, 30), (150, 150)], closed=False)
        self.win.check_missing_corners(announce=False)
        self.assertTrue(self.win.canvas.edge_warnings())
        # Re-tracing / editing invalidates the warnings.
        self.win.canvas.start_polygon()
        self.win.canvas._place_point(QPointF(80, 40))
        self.assertEqual(self.win.canvas.edge_warnings(), set())


@unittest.skipUnless(_HAVE_QT and _HAVE_EZDXF, "PySide6 / ezdxf not available")
class DxfSkipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dxf = Path(self._tmp.name) / "plan.dxf"
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 6
        msp = doc.modelspace()
        for a, b in [((0, 0), (100, 0)), ((100, 0), (100, 100)),
                     ((100, 100), (0, 100)), ((0, 100), (0, 0))]:
            msp.add_line(a, b)
        doc.saveas(str(self.dxf))
        self.win = MainWindow()
        self.win.load_path(str(self.dxf))

    def tearDown(self):
        self.win.close()
        self._tmp.cleanup()

    def test_missing_corner_check_is_skipped_for_dxf(self):
        # Even a blatant cut-corner produces no warnings on a DXF source.
        self.win.canvas.set_polygon([(50, 50), (900, 900)], closed=False)
        self.win.check_missing_corners(announce=False)
        self.assertEqual(self.win.canvas.edge_warnings(), set())
        self.assertIn("dxf", self.win._status.text().lower())


if __name__ == "__main__":
    unittest.main()
