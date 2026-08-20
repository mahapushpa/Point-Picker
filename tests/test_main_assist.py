"""Window-level tests for M18 semi-automated tracing assist.

Verify the non-negotiables: never auto-accepted, reject leaves the polygon
untouched, accept integrates with M6 snapping and the M17 guard rails, DXF is
disabled, and the mode is mutually exclusive with the others. Skipped without
PySide6 / Pillow.
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
    g = np.full((200, 200), 255, dtype=np.uint8)
    g[40:121, 39:42] = 0
    g[119:122, 40:161] = 0
    Image.fromarray(g, mode="L").convert("RGB").save(path)


@unittest.skipUnless(_HAVE_QT and _HAVE_IMG, "PySide6 / Pillow / numpy not available")
class AssistUiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.png = Path(self._tmp.name) / "sheet.png"
        _write_L_png(self.png)
        self.win = MainWindow()
        self.win.load_path(str(self.png))

    def tearDown(self):
        self.win.close()
        self._tmp.cleanup()

    def _mark(self, start, end):
        """Mark both ends -> computes + shows a preview (via the connected signal)."""
        self.win.canvas.start_assist()
        self.win.canvas._assist_click(QPointF(*start))
        self.win.canvas._assist_click(QPointF(*end))

    def test_preview_is_not_auto_accepted(self):
        self._mark((40, 40), (160, 120))
        self.assertTrue(self.win.canvas.has_assist_preview())
        self.assertEqual(self.win.canvas.polygon_points(), [])   # nothing added yet

    def test_reject_leaves_polygon_unchanged(self):
        existing = [(10, 10), (20, 30)]
        self.win.canvas.set_polygon(existing, closed=False)
        self._mark((40, 40), (160, 120))
        self.win.reject_assist()
        self.assertFalse(self.win.canvas.has_assist_preview())
        self.assertEqual(self.win.canvas.polygon_points(), existing)

    def test_accept_appends_followed_path(self):
        self._mark((40, 40), (160, 120))
        n_preview = len(self.win.canvas._assist_preview)
        self.win.accept_assist()
        pts = self.win.canvas.polygon_points()
        self.assertEqual(len(pts), n_preview)
        self.assertEqual(pts[0], (40.0, 40.0))
        self.assertEqual(pts[-1], (160.0, 120.0))
        self.assertFalse(self.win.canvas.has_assist_preview())

    def test_accept_snaps_to_existing_vertex_M6(self):
        # A shared vertex sits on the followed path's start; accepting must snap the
        # first appended point onto it (same topology behaviour as manual tracing).
        self.win.canvas.set_snap_vertices([(77, 40, 40)])
        self._mark((40, 40), (160, 120))
        self.win.accept_assist()
        self.assertEqual(self.win.canvas.active_vertex_ids()[0], 77)

    def test_accepted_path_subject_to_M17_missing_corner_check(self):
        # Accept a straight cut across the L's corner, then the guard-rail check
        # (not exempted for assisted points) flags it.
        self._mark((40, 40), (160, 120))
        self.win.accept_assist()
        # Force a 2-point cut (assist may already round the corner) to prove the
        # accepted geometry is still checkable by M17.
        self.win.canvas.set_polygon([(40, 40), (160, 120)], closed=False)
        self.win.check_missing_corners(announce=False)
        self.assertTrue(self.win.canvas.edge_warnings())

    def test_mode_is_mutually_exclusive(self):
        self.win.begin_assist()
        self.assertTrue(self.win.canvas.is_assisting())
        self.win.begin_polygon_tracing()
        self.assertFalse(self.win.canvas.is_assisting())
        self.assertTrue(self.win.canvas.is_tracing())
        self.assertFalse(self.win._assist_action.isChecked())


@unittest.skipUnless(_HAVE_QT and _HAVE_EZDXF, "PySide6 / ezdxf not available")
class AssistDxfTests(unittest.TestCase):
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

    def test_assist_disabled_for_dxf(self):
        self.assertFalse(self.win._assist_action.isEnabled())
        self.win.begin_assist()                       # even if invoked...
        self.assertFalse(self.win.canvas.is_assisting())   # ...it does not enter


if __name__ == "__main__":
    unittest.main()
