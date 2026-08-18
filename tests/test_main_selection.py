"""Headless tests for Milestone 7 parcel multi-selection at the window level.

Builds a real MainWindow with a temp project and two parcels, then drives the
selection mechanism the way the UI does — canvas signals, sidebar checkboxes,
and the select-all/clear actions — asserting selection is a working set kept
strictly independent of the active (editable) parcel. Runs offscreen; skipped if
PySide6 is unavailable.
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
    from PySide6.QtCore import QPointF, Qt
    from PIL import Image
    from src.ui.main_window import MainWindow
    from src.core.project_db import ProjectDB
    _HAVE_QT = True
except Exception:  # pragma: no cover - environment without PySide6/Pillow
    _HAVE_QT = False

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])
        # Silence any modal dialogs the window might raise.
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class SelectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        img = tmp / "sheet.png"
        Image.new("RGB", (500, 400), (210, 205, 195)).save(img)
        proj = tmp / "proj"
        proj.mkdir()

        self.w = MainWindow()
        self.w._set_project(ProjectDB.create(str(proj)))
        self.w.load_path(str(img))

        # Two well-separated square parcels so hit-testing is unambiguous.
        self.pa = self._trace([(0, 0), (100, 0), (100, 100), (0, 100)])
        self.pb = self._trace([(200, 0), (300, 0), (300, 100), (200, 100)])

    def tearDown(self):
        self.w.close()
        self._tmp.cleanup()

    def _trace(self, pts):
        self.w.new_parcel()
        for x, y in pts:
            self.w.canvas._place_point(QPointF(x, y))
        self.w.close_polygon()
        return self.w._active_parcel_id

    # -- toggle select / deselect -------------------------------------------

    def test_toggle_selects_then_deselects(self):
        self.assertEqual(self.w.selected_parcel_ids(), [])
        self.w.toggle_parcel_selection(self.pa)
        self.assertEqual(self.w.selected_parcel_ids(), [self.pa])
        self.w.toggle_parcel_selection(self.pa)
        self.assertEqual(self.w.selected_parcel_ids(), [])

    def test_canvas_click_toggles_parcel_under_cursor(self):
        # A click inside parcel A's fill selects it; clicking empty space is a no-op.
        self.w.canvas.selectionClicked.emit(QPointF(50, 50))
        self.assertEqual(self.w.selected_parcel_ids(), [self.pa])
        self.w.canvas.selectionClicked.emit(QPointF(400, 350))  # empty
        self.assertEqual(self.w.selected_parcel_ids(), [self.pa])
        self.w.canvas.selectionClicked.emit(QPointF(50, 50))    # toggle back off
        self.assertEqual(self.w.selected_parcel_ids(), [])

    def test_selection_reflected_on_canvas_and_count(self):
        self.w.toggle_parcel_selection(self.pb)
        self.assertEqual(self.w.canvas.selected_ids(), {self.pb})
        self.assertIn("1 parcel selected", self.w._selection_label.text())

    # -- marquee ------------------------------------------------------------

    def test_marquee_selects_multiple(self):
        # A rectangle spanning both parcels catches both.
        self.w.canvas.marqueeSelected.emit(-10, -10, 320, 120)
        self.assertEqual(set(self.w.selected_parcel_ids()), {self.pa, self.pb})

    def test_marquee_catches_only_overlapping_parcels(self):
        # A rectangle only over parcel B.
        self.w.canvas.marqueeSelected.emit(190, -10, 320, 120)
        self.assertEqual(self.w.selected_parcel_ids(), [self.pb])

    def test_marquee_is_additive(self):
        self.w.toggle_parcel_selection(self.pa)
        self.w.canvas.marqueeSelected.emit(190, -10, 320, 120)  # adds B
        self.assertEqual(set(self.w.selected_parcel_ids()), {self.pa, self.pb})

    # -- select all / clear -------------------------------------------------

    def test_select_all_then_clear(self):
        self.w.select_all_parcels()
        self.assertEqual(set(self.w.selected_parcel_ids()), {self.pa, self.pb})
        self.w.clear_selection()
        self.assertEqual(self.w.selected_parcel_ids(), [])

    # -- sidebar checkbox ---------------------------------------------------

    def test_sidebar_checkbox_drives_selection(self):
        row = self.w._parcel_index(self.pa)
        item = self.w._parcel_list.item(row)
        item.setCheckState(Qt.CheckState.Checked)   # fires itemChanged
        self.assertEqual(self.w.selected_parcel_ids(), [self.pa])
        item.setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(self.w.selected_parcel_ids(), [])

    def test_selection_sets_checkbox(self):
        self.w.toggle_parcel_selection(self.pb)
        row = self.w._parcel_index(self.pb)
        item = self.w._parcel_list.item(row)
        self.assertEqual(item.checkState(), Qt.CheckState.Checked)

    # -- independence from the active parcel --------------------------------

    def test_changing_active_parcel_does_not_change_selection(self):
        self.w.select_all_parcels()
        before = set(self.w.selected_parcel_ids())
        # Switch the active parcel via the sidebar current row.
        self.w._parcel_list.setCurrentRow(self.w._parcel_index(self.pa))
        self.assertEqual(self.w._active_parcel_id, self.pa)
        self.w._parcel_list.setCurrentRow(self.w._parcel_index(self.pb))
        self.assertEqual(self.w._active_parcel_id, self.pb)
        self.assertEqual(set(self.w.selected_parcel_ids()), before)

    def test_changing_selection_does_not_change_active_parcel(self):
        self.w._parcel_list.setCurrentRow(self.w._parcel_index(self.pa))
        active_before = self.w._active_parcel_id
        self.w.toggle_parcel_selection(self.pb)   # select the *other* parcel
        self.assertEqual(self.w._active_parcel_id, active_before)
        self.assertNotIn(self.w._active_parcel_id, [])  # sanity
        # The active parcel need not be part of the selection.
        self.assertNotIn(active_before, self.w.selected_parcel_ids())

    def test_active_parcel_can_be_unselected(self):
        # Active is pa; select only pb. Active is not in the selection.
        self.w._parcel_list.setCurrentRow(self.w._parcel_index(self.pa))
        self.w.toggle_parcel_selection(self.pb)
        self.assertEqual(self.w._active_parcel_id, self.pa)
        self.assertEqual(self.w.selected_parcel_ids(), [self.pb])

    # -- select mode discoverability + toggle -------------------------------

    def test_select_toolbar_action_is_checkable_with_clear_label(self):
        act = self.w._select_action
        self.assertTrue(act.isCheckable())               # a visible latch, not a one-shot
        self.assertIn("select", act.text().lower())      # label names the action
        self.assertIn("parcel", act.text().lower())
        self.assertTrue(act.toolTip())                   # explains click + marquee

    def test_begin_selection_latches_toggle_and_enters_mode(self):
        self.assertFalse(self.w._select_action.isChecked())
        self.w.begin_selection()
        self.assertTrue(self.w._select_action.isChecked())
        self.assertTrue(self.w.canvas.is_selecting())
        self.assertIn("select mode", self.w._status.text().lower())  # hint shown in the UI

    def test_toggling_off_exits_mode_but_keeps_selection(self):
        self.w.begin_selection()
        self.w.toggle_parcel_selection(self.pa)          # pick one while in mode
        self.w._select_action.setChecked(False)          # user clicks the toggle off
        self.assertFalse(self.w.canvas.is_selecting())
        self.assertEqual(self.w.selected_parcel_ids(), [self.pa])  # selection persists

    def test_switching_to_trace_unchecks_select_toggle(self):
        self.w.begin_selection()
        self.assertTrue(self.w._select_action.isChecked())
        self.w.begin_polygon_tracing()
        self.assertFalse(self.w._select_action.isChecked())
        self.assertFalse(self.w.canvas.is_selecting())

    # -- lifecycle ----------------------------------------------------------

    def test_deleting_selected_parcel_prunes_it(self):
        self.w.select_all_parcels()
        self.w._parcel_list.setCurrentRow(self.w._parcel_index(self.pa))
        self.w.delete_active_parcel()
        self.assertEqual(self.w.selected_parcel_ids(), [self.pb])
        self.assertNotIn(self.pa, self.w.canvas.selected_ids())


if __name__ == "__main__":
    unittest.main()
