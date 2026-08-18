"""Headless window-level tests for the M8 preprocessing preview toggle.

The toggle is a display-time transform only: it must never alter stored scale or
vertex data, nor the live canvas coordinates, since preprocessing keeps pixel
coordinates identical. Runs offscreen; skipped if PySide6/Pillow are unavailable.
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
    import numpy as np
    from PySide6.QtWidgets import QApplication, QMessageBox, QInputDialog
    from PySide6.QtCore import QPointF
    from PIL import Image
    from src.ui.main_window import MainWindow
    from src.core.project_db import ProjectDB
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)


@unittest.skipUnless(_HAVE_QT, "PySide6/Pillow not available")
class PreprocessToggleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        # A structured, low-contrast grayscale scan so enhancement is a real change.
        yy, xx = np.mgrid[0:300, 0:400]
        g = np.clip(128 + (np.sin(xx / 8.0) + np.cos(yy / 11.0)) * 10, 0, 255).astype(np.uint8)
        self._img = tmp / "sheet.png"
        Image.fromarray(g, mode="L").convert("RGB").save(self._img)
        proj = tmp / "proj"
        proj.mkdir()

        self.w = MainWindow()
        self.w._set_project(ProjectDB.create(str(proj)))
        self.w.load_path(str(self._img))

        # A scale (two points 100 px apart == 10 m) and one traced parcel.
        QInputDialog.getDouble = staticmethod(lambda *a, **k: (10.0, True))
        self.w.begin_scale_calibration()
        self.w.canvas._place_point(QPointF(50, 50))
        self.w.canvas._place_point(QPointF(150, 50))
        self.w.canvas.confirm_pick()

        self.pid = self._trace([(20, 20), (120, 20), (120, 120), (20, 120)])

    def tearDown(self):
        self.w.close()
        self._tmp.cleanup()

    def _trace(self, pts):
        self.w.new_parcel()
        for x, y in pts:
            self.w.canvas._place_point(QPointF(x, y))
        self.w.close_polygon()
        return self.w._active_parcel_id

    def _snapshot(self):
        sid = self.w._source_id
        scale = self.w._project.get_source_scale(sid)
        verts = self.w._project.list_vertices(sid)
        poly = self.w._project.get_parcel_polygon(self.pid)
        canvas_pts = self.w.canvas.polygon_points()
        return scale, verts, poly, canvas_pts

    def test_toggle_preserves_dimensions_scale_and_vertices(self):
        scale0, verts0, poly0, cpts0 = self._snapshot()
        raw_dims = (self.w._raw_raster.width, self.w._raw_raster.height)

        self.w.set_preprocess_enabled(True)       # show enhanced
        self.assertTrue(self.w._preprocess_on)
        # Enhanced raster has identical dimensions (value-only transform).
        self.assertEqual((self.w._pre_raster.width, self.w._pre_raster.height), raw_dims)

        scale1, verts1, poly1, cpts1 = self._snapshot()
        self.assertEqual(scale1["metres_per_pixel"], scale0["metres_per_pixel"])
        self.assertEqual(verts1, verts0)          # stored vertices byte-for-byte identical
        self.assertEqual(poly1, poly0)
        self.assertEqual(cpts1, cpts0)            # live canvas coordinates unchanged

        self.w.set_preprocess_enabled(False)      # back to original
        scale2, verts2, poly2, cpts2 = self._snapshot()
        self.assertEqual(verts2, verts0)
        self.assertEqual(poly2, poly0)
        self.assertEqual(cpts2, cpts0)

    def test_enhanced_display_actually_differs_from_raw(self):
        # Sanity: the toggle is doing something visible (else the test above is
        # vacuous). Compare the raster the canvas would show in each state.
        self.w.set_preprocess_enabled(True)
        raw = np.frombuffer(self.w._raw_raster.data, dtype=np.uint8)
        pre = np.frombuffer(self.w._pre_raster.data, dtype=np.uint8)
        self.assertEqual(raw.shape, pre.shape)
        self.assertFalse(np.array_equal(raw, pre), "enhanced pixels should differ from raw")

    def test_tracing_works_while_enhanced(self):
        # Snapping / tracing depends only on pixel coordinates, so it must behave
        # the same with the preview on. Trace a second parcel that shares a corner.
        self.w.set_preprocess_enabled(True)
        pid2 = self._trace([(120, 20), (220, 20), (220, 120), (120, 120)])
        shared = set(self.w._project.get_parcel_vertex_ids(self.pid)) & \
            set(self.w._project.get_parcel_vertex_ids(pid2))
        self.assertTrue(shared, "adjacent parcel should still snap to a shared vertex when enhanced")


if __name__ == "__main__":
    unittest.main()
