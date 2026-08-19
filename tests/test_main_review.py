"""Window-level tests for the M13 visual confidence overlay.

Drives MainWindow offscreen to check the review-only display controls:
  * the global show/hide of all overlays leaves stored geometry and the active
    tool/mode untouched;
  * per-parcel overlay hiding is independent of the active/selected state;
  * opacity changes are purely visual (no effect on stored geometry).

These are display concerns, so they are asserted on the canvas's drawn-item
buckets and public overlay API, while the parcel geometry is checked against the
DB. Skipped without PySide6 / Pillow.
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
    from src.io.raster import open_raster
    from src.ui.main_window import MainWindow, _HIDDEN_ROLE
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False

try:
    from PIL import Image
    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)


SQUARE_A = [(10, 10), (110, 10), (110, 110), (10, 110)]
SQUARE_B = [(130, 130), (190, 130), (190, 190), (130, 190)]


@unittest.skipUnless(_HAVE_QT and _HAVE_PIL, "PySide6 / Pillow not available")
class OverlayReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.proj = ProjectDB.create(str(root / "proj"), name="Demo")
        self.png = root / "sheet.png"
        Image.new("RGB", (200, 200), (255, 255, 255)).save(self.png)
        self.sid = self.proj.import_source(self.png, "image")

        self.win = MainWindow()
        self.win._set_project(self.proj)
        self.win._source_id = self.sid
        self.win.canvas.set_image(open_raster(self.png))

        # Two closed parcels on one sheet: A is made active, B is context.
        self.proj.set_source_scale(self.sid, 0.5, method="two-point", note="ok")
        self.pid_a = self.proj.create_parcel(self.sid, owner="Ramesh")
        self.proj.save_parcel_polygon(self.pid_a, SQUARE_A, closed=True, metres_per_pixel=0.5)
        self.pid_b = self.proj.create_parcel(self.sid, owner="Suresh")
        self.proj.save_parcel_polygon(self.pid_b, SQUARE_B, closed=True, metres_per_pixel=0.5)
        # Full reload so the Parcels list widget is populated (the eye toggle and
        # row cues live on real list items), with A active and B as context.
        self.win._reload_parcels(select_id=self.pid_a)

    def tearDown(self):
        self.win.close()
        try:
            self.proj.close()
        except Exception:
            pass
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def _active_item_count(self):
        return len(self.win.canvas._poly_items)

    def _background_item_count(self):
        return len(self.win.canvas._bg_items)

    def _db_polys(self):
        return (self.proj.get_parcel_polygon(self.pid_a),
                self.proj.get_parcel_polygon(self.pid_b))

    # -- global toggle ------------------------------------------------------

    def test_global_toggle_hides_all_overlays_then_restores(self):
        # Both parcels are drawn to start with.
        self.assertGreater(self._active_item_count(), 0)
        self.assertGreater(self._background_item_count(), 0)

        # Enter a live editing mode and record state we expect to survive.
        self.win.begin_polygon_tracing()
        self.assertTrue(self.win.canvas.is_tracing())
        pts_before = self.win.canvas.polygon_points()
        db_before = self._db_polys()

        # Hide everything via the toolbar action (the real UI path).
        self.win._overlays_action.setChecked(False)
        self.assertFalse(self.win.canvas.overlays_visible())
        self.assertEqual(self._active_item_count(), 0)
        self.assertEqual(self._background_item_count(), 0)

        # Mode is preserved and NOTHING stored changed.
        self.assertTrue(self.win.canvas.is_tracing())
        self.assertEqual(self.win.canvas.polygon_points(), pts_before)
        self.assertEqual(self._db_polys(), db_before)

        # Showing again brings every overlay back.
        self.win._overlays_action.setChecked(True)
        self.assertTrue(self.win.canvas.overlays_visible())
        self.assertGreater(self._active_item_count(), 0)
        self.assertGreater(self._background_item_count(), 0)
        self.assertTrue(self.win.canvas.is_tracing())

    def test_global_toggle_preserves_segment_mode_and_selection(self):
        self.win.begin_segment_description()
        self.assertTrue(self.win.canvas.is_segment_selecting())
        self.win.canvas.set_segment_selection([0, 1])

        self.win._overlays_action.setChecked(False)
        # Segment mode and its selection survive a purely-visual hide.
        self.assertTrue(self.win.canvas.is_segment_selecting())
        self.assertEqual(self.win.canvas.selected_segment_edges(), [0, 1])

        self.win._overlays_action.setChecked(True)
        self.assertEqual(self.win.canvas.selected_segment_edges(), [0, 1])

    # -- per-parcel visibility ---------------------------------------------

    def test_per_parcel_hide_is_independent_of_active_and_selected(self):
        # Put B in the selection working set (a distinct state from active).
        self.win._selected_parcel_ids = {self.pid_b}
        self.win._refresh_backgrounds()
        db_before = self._db_polys()

        # Hide B (the background parcel); A (active) stays drawn.
        self.win.toggle_parcel_overlay(self.pid_b)
        self.assertTrue(self.win.canvas.is_parcel_hidden(self.pid_b))
        self.assertEqual(self._background_item_count(), 0)   # B was the only background
        self.assertGreater(self._active_item_count(), 0)     # A unaffected

        # Neither active nor selected state changed, and no geometry moved.
        self.assertEqual(self.win._active_parcel_id, self.pid_a)
        self.assertIn(self.pid_b, self.win._selected_parcel_ids)
        self.assertIn(self.pid_b, self.win.canvas.selected_ids())
        self.assertEqual(self._db_polys(), db_before)

        # The hide persists even when B becomes the active parcel: hiding is
        # genuinely per-parcel, not a function of active state.
        self.win._set_active_parcel(self.pid_b)
        self.assertTrue(self.win.canvas.is_parcel_hidden(self.pid_b))
        self.assertEqual(self._active_item_count(), 0)       # active B still hidden
        self.assertGreater(self._background_item_count(), 0)  # A now shown as context

        # Show-all clears every per-parcel hide.
        self.win.show_all_parcel_overlays()
        self.assertFalse(self.win.canvas.is_parcel_hidden(self.pid_b))
        self.assertGreater(self._active_item_count(), 0)
        self.assertEqual(self._db_polys(), db_before)

    def test_hiding_active_parcel_keeps_it_active_and_editable_data(self):
        pts_before = self.win.canvas.polygon_points()
        self.win.toggle_parcel_overlay(self.pid_a)   # hide the ACTIVE parcel
        self.assertTrue(self.win.canvas.is_parcel_hidden(self.pid_a))
        self.assertEqual(self._active_item_count(), 0)
        # Still the active parcel, with its points intact — only the drawing hid.
        self.assertEqual(self.win._active_parcel_id, self.pid_a)
        self.assertEqual(self.win.canvas.polygon_points(), pts_before)

    # -- per-row eye toggle (discoverability affordance) --------------------

    def _row_hidden_state(self, pid):
        """(canvas-hidden, row shows the '(overlay hidden)' cue, delegate role) for
        a parcel — the three things that must always agree."""
        row = self.win._parcel_index(pid)
        item = self.win._parcel_list.item(row)
        return (self.win.canvas.is_parcel_hidden(pid),
                "(overlay hidden)" in item.text(),
                bool(item.data(_HIDDEN_ROLE)))

    def test_eye_click_toggles_visibility_like_the_context_menu(self):
        row_b = self.win._parcel_index(self.pid_b)
        self.assertEqual(self._row_hidden_state(self.pid_b), (False, False, False))

        # Eye click hides — same effect as toggle_parcel_overlay (the menu path).
        self.win._on_parcel_eye_clicked(row_b)
        self.assertEqual(self._row_hidden_state(self.pid_b), (True, True, True))
        self.assertEqual(self._background_item_count(), 0)   # B's overlay gone

        # Eye click again shows.
        self.win._on_parcel_eye_clicked(row_b)
        self.assertEqual(self._row_hidden_state(self.pid_b), (False, False, False))
        self.assertGreater(self._background_item_count(), 0)

    def test_eye_and_context_menu_paths_stay_in_sync(self):
        row_b = self.win._parcel_index(self.pid_b)
        # Hide via the right-click/menu path...
        self.win.toggle_parcel_overlay(self.pid_b)
        self.assertEqual(self._row_hidden_state(self.pid_b), (True, True, True))
        # ...and show via the eye path: the menu path's displayed state updates too.
        self.win._on_parcel_eye_clicked(row_b)
        self.assertEqual(self._row_hidden_state(self.pid_b), (False, False, False))

        # And the reverse ordering: eye hides, menu shows.
        self.win._on_parcel_eye_clicked(row_b)
        self.assertEqual(self._row_hidden_state(self.pid_b), (True, True, True))
        self.win.toggle_parcel_overlay(self.pid_b)
        self.assertEqual(self._row_hidden_state(self.pid_b), (False, False, False))

    def test_eye_state_survives_an_owner_edit_row_refresh(self):
        # Hiding, then a row refresh (e.g. owner/point-count change) must keep the
        # cue and eye state — the shared row-updater guarantees this.
        row_b = self.win._parcel_index(self.pid_b)
        self.win._on_parcel_eye_clicked(row_b)
        self.win._update_parcel_list_row(row_b)
        self.assertEqual(self._row_hidden_state(self.pid_b), (True, True, True))

    # -- opacity ------------------------------------------------------------

    def test_opacity_is_purely_visual(self):
        db_before = self._db_polys()
        pts_before = self.win.canvas.polygon_points()

        self.win._opacity_slider.setValue(50)
        self.assertAlmostEqual(self.win.canvas.overlay_opacity(), 0.5)
        self.assertEqual(self.win._opacity_label.text(), "50%")
        # Drawn overlay items are faded...
        self.assertAlmostEqual(self.win.canvas._poly_items[0].opacity(), 0.5)
        self.assertAlmostEqual(self.win.canvas._bg_items[0].opacity(), 0.5)
        # ...but geometry is untouched, in memory and in the DB.
        self.assertEqual(self.win.canvas.polygon_points(), pts_before)
        self.assertEqual(self._db_polys(), db_before)

        # Back to full opacity restores solid overlays.
        self.win._opacity_slider.setValue(100)
        self.assertAlmostEqual(self.win.canvas.overlay_opacity(), 1.0)
        self.assertAlmostEqual(self.win.canvas._poly_items[0].opacity(), 1.0)
        self.assertEqual(self._db_polys(), db_before)


if __name__ == "__main__":
    unittest.main()
