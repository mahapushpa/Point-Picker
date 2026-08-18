"""Headless tests for src.ui.canvas_view point editing (M4.5 refinements).

Exercise the shared place / adjust / confirm / cancel logic without a window:
the scene-coordinate editing helpers are driven directly, so results don't
depend on view layout or coordinate mapping. Runs offscreen; skipped if PySide6
is unavailable.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QPointF
    from src.ui.canvas_view import CanvasView
    from src.io.raster import RasterImage
    _HAVE_QT = True
except Exception:  # pragma: no cover - environment without PySide6
    _HAVE_QT = False

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class CanvasPointEditingTests(unittest.TestCase):
    def _canvas(self, w=200, h=150):
        c = CanvasView()
        c.set_image(RasterImage(w, h, bytes(w * h * 4), "RGBA"))
        return c

    # -- fine-tune: drag adjusts a placed point -----------------------------

    def test_polygon_drag_adjusts_stored_point(self):
        c = self._canvas()
        self.assertTrue(c.start_polygon())
        for p in [(10, 10), (50, 10), (50, 50)]:
            c._place_point(QPointF(*p))
        self.assertEqual(c.polygon_points(), [(10, 10), (50, 10), (50, 50)])
        # Simulate grabbing vertex 2 and dragging it to a new spot.
        c._drag_kind, c._drag_index = "poly", 2
        c._set_marker_position("poly", 2, QPointF(60, 70))
        c._drag_kind = c._drag_index = None
        self.assertEqual(c.polygon_points()[2], (60, 70))  # stored coord changed

    def test_scale_drag_adjusts_point_before_confirm(self):
        c = self._canvas()
        self.assertTrue(c.start_scale_calibration())
        emitted = []
        c.twoPointsPicked.connect(lambda a, b: emitted.append((a, b)))
        c._place_point(QPointF(20, 20))
        c._place_point(QPointF(120, 20))
        self.assertEqual(emitted, [])              # deferred: not finalised on 2nd click
        self.assertTrue(c.is_calibrating())
        # Fine-tune point 1, then confirm.
        c._drag_kind, c._drag_index = "scale", 0
        c._set_marker_position("scale", 0, QPointF(30, 25))
        c._drag_kind = c._drag_index = None
        self.assertTrue(c.confirm_pick())
        self.assertEqual(len(emitted), 1)
        a, b = emitted[0]
        self.assertEqual((a.x(), a.y()), (30, 25))  # adjusted point fed to the math
        self.assertEqual((b.x(), b.y()), (120, 20))
        self.assertFalse(c.is_calibrating())

    # -- fine-tune: arrow-key nudge -----------------------------------------

    def test_nudge_changes_active_point(self):
        c = self._canvas()
        c.start_polygon()
        c._place_point(QPointF(10, 10))            # this becomes the active point
        c._nudge_active(1, 0)                      # right 1 px
        self.assertEqual(c.polygon_points()[0], (11, 10))
        c._nudge_active(0, 10)                     # down 10 px (shift-sized step)
        self.assertEqual(c.polygon_points()[0], (11, 20))

    def test_nudge_targets_the_selected_point(self):
        c = self._canvas()
        c.start_polygon()
        for p in [(0, 0), (10, 0), (20, 0)]:
            c._place_point(QPointF(*p))
        c._active = ("poly", 1)                    # select the middle vertex
        c._nudge_active(0, -5)
        self.assertEqual(c.polygon_points(), [(0, 0), (10, -5), (20, 0)])

    # -- reliable cancel at every stage -------------------------------------

    def test_cancel_scale_after_first_point(self):
        c = self._canvas()
        c.start_scale_calibration()
        c._place_point(QPointF(20, 20))
        self.assertEqual(len(c._calib_points), 1)
        self.assertTrue(c.cancel_pick())
        self.assertEqual(c._calib_points, [])
        self.assertEqual(c._calib_items, [])       # no stray marker items
        self.assertFalse(c.is_calibrating())
        self.assertIsNone(c._active)

    def test_cancel_scale_after_second_point(self):
        c = self._canvas()
        c.start_scale_calibration()
        c._place_point(QPointF(0, 0))
        c._place_point(QPointF(10, 0))
        self.assertTrue(c.cancel_pick())
        self.assertEqual(c._calib_points, [])
        self.assertEqual(c._calib_items, [])
        self.assertFalse(c.is_calibrating())

    def test_cancel_polygon_clears_fully(self):
        c = self._canvas()
        c.start_polygon()
        for p in [(0, 0), (10, 0), (10, 10), (0, 10)]:
            c._place_point(QPointF(*p))
        self.assertEqual(len(c.polygon_points()), 4)
        self.assertTrue(c.cancel_pick())
        self.assertEqual(c.polygon_points(), [])
        self.assertEqual(c._poly_items, [])
        self.assertFalse(c.is_tracing())
        self.assertFalse(c.is_polygon_closed())
        self.assertIsNone(c._active)

    def test_extra_scale_clicks_ignored_after_two(self):
        c = self._canvas()
        c.start_scale_calibration()
        c._place_point(QPointF(0, 0))
        c._place_point(QPointF(10, 0))
        c._place_point(QPointF(5, 5))              # a 3rd click must not add a point
        self.assertEqual(len(c._calib_points), 2)

    # -- mode interplay & crosshair -----------------------------------------

    def test_switch_to_scale_keeps_polygon_but_clears_calibration(self):
        c = self._canvas()
        c.start_polygon()
        for p in [(1, 1), (2, 2), (3, 1)]:
            c._place_point(QPointF(*p))
        c.start_scale_calibration()                # boundary should survive
        self.assertEqual(len(c.polygon_points()), 3)
        c._place_point(QPointF(0, 0))
        c._place_point(QPointF(9, 0))
        c.start_polygon()                          # switching back clears calibration
        self.assertEqual(c._calib_points, [])
        self.assertEqual(len(c.polygon_points()), 3)

    def test_confirm_polygon_closes(self):
        c = self._canvas()
        c.start_polygon()
        for p in [(0, 0), (10, 0), (5, 8)]:
            c._place_point(QPointF(*p))
        self.assertTrue(c.confirm_pick())
        self.assertTrue(c.is_polygon_closed())

    def test_confirm_noop_without_enough_points(self):
        c = self._canvas()
        c.start_scale_calibration()
        c._place_point(QPointF(0, 0))
        self.assertFalse(c.confirm_pick())         # only 1 point -> nothing happens
        self.assertTrue(c.is_calibrating())

    def test_crosshair_defaults_per_mode(self):
        c = self._canvas()
        c.start_scale_calibration()
        self.assertTrue(c.is_crosshair_enabled())   # on for scale
        c.start_polygon()
        self.assertFalse(c.is_crosshair_enabled())  # off for polygon
        c.toggle_crosshair()
        self.assertTrue(c.is_crosshair_enabled())


if __name__ == "__main__":
    unittest.main()
