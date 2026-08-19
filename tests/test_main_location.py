"""Window-level tests for M16 location-fixing.

Cover the UI contracts: scale-first blocking, mode mutual-exclusivity, M7
selection-scoping of the target parcel, and that field/sheet observations persist
and drive the cross-check. The distance/bearing/cross-validation math itself is
covered by test_location.py. Skipped without PySide6 / Pillow.
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
    from src.core.scale import compute_two_point_scale
    from src.io.raster import open_raster
    from src.ui.main_window import MainWindow
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


SQ_A = [(10, 10), (110, 10), (110, 110), (10, 110)]
SQ_B = [(200, 200), (260, 200), (260, 260), (200, 260)]


@unittest.skipUnless(_HAVE_QT and _HAVE_PIL, "PySide6 / Pillow not available")
class LocationUiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.proj = ProjectDB.create(str(root / "proj"), name="Demo")
        self.png = root / "sheet.png"
        Image.new("RGB", (400, 400), (255, 255, 255)).save(self.png)

        self.win = MainWindow()
        self.win._set_project(self.proj)
        self.win.load_path(str(self.png))
        self.sid = self.win._source_id
        self.pid_a = self.proj.create_parcel(self.sid, owner="Ramesh")
        self.proj.save_parcel_polygon(self.pid_a, SQ_A, closed=True, metres_per_pixel=None)
        self.pid_b = self.proj.create_parcel(self.sid, owner="Suresh")
        self.proj.save_parcel_polygon(self.pid_b, SQ_B, closed=True, metres_per_pixel=None)
        self.win._reload_parcels(select_id=self.pid_a)

    def tearDown(self):
        self.win.close()
        try:
            self.proj.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _set_scale(self, mpp=0.5):
        self.proj.set_source_scale(self.sid, mpp, method="two-point", note="ok")
        self.win._scale = compute_two_point_scale((0, 0), (1 / mpp, 0), 1.0)  # mpp per px

    def _select(self, *pids):
        self.win._selected_parcel_ids = set(pids)

    # -- scale-first --------------------------------------------------------

    def test_scale_first_blocks_when_no_scale(self):
        self._select(self.pid_a)
        self.win.begin_location_fix()
        self.assertFalse(self.win._location_action.isChecked())
        self.assertFalse(self.win.canvas.is_locating())

    def test_enters_mode_when_scale_and_parcel_present(self):
        self._set_scale()
        self._select(self.pid_a)
        self.win.begin_location_fix()
        self.assertTrue(self.win._location_action.isChecked())
        self.assertTrue(self.win.canvas.is_locating())

    # -- selection scoping reuses M7 ---------------------------------------

    def test_target_parcel_comes_from_selection_not_active(self):
        self._set_scale()
        self.win._set_active_parcel(self.pid_a)     # A is active...
        self._select(self.pid_b)                    # ...but only B is selected
        self.win.begin_location_fix()
        self.assertEqual(self.win._location_parcel_id, self.pid_b)

    def test_no_parcel_selected_or_active_blocks(self):
        self._set_scale()
        self._select()                              # empty selection
        self.win._active_parcel_id = None
        self.win.begin_location_fix()
        self.assertFalse(self.win.canvas.is_locating())

    # -- mode mutual-exclusivity -------------------------------------------

    def test_entering_location_leaves_other_modes(self):
        self._set_scale()
        self._select(self.pid_a)
        self.win.begin_selection()                  # in Select mode
        self.assertTrue(self.win.canvas.is_selecting())
        self.win.begin_location_fix()
        self.assertTrue(self.win.canvas.is_locating())
        self.assertFalse(self.win.canvas.is_selecting())
        self.assertFalse(self.win._select_action.isChecked())

    def test_entering_another_mode_leaves_location(self):
        self._set_scale()
        self._select(self.pid_a)
        self.win.begin_location_fix()
        self.assertTrue(self.win.canvas.is_locating())
        self.win.begin_polygon_tracing()            # switch to Trace
        self.assertTrue(self.win.canvas.is_tracing())
        self.assertFalse(self.win.canvas.is_locating())
        self.assertFalse(self.win._location_action.isChecked())

    # -- observations persist + drive the table/cross-check ----------------

    def test_field_observation_persists_and_computes_target(self):
        self._set_scale(0.5)
        self._select(self.pid_a)
        self.win.begin_location_fix()
        self.win.add_location_reference_field((100.0, 100.0), "tubewell", 20.0, 90.0)
        fixes = self.proj.list_location_fixes(self.pid_a)
        self.assertEqual(len(fixes), 1)
        fx = fixes[0]
        self.assertEqual(fx["source"], "field")
        self.assertAlmostEqual(fx["distance_m"], 20.0)
        self.assertAlmostEqual(fx["bearing_deg"], 90.0)
        # 20 m East at 0.5 m/px = +40 px x (trig-located target).
        self.assertAlmostEqual(fx["target_x"], 140.0)
        self.assertAlmostEqual(fx["target_y"], 100.0)
        self.assertEqual(self.win._location_table.rowCount(), 1)

    def test_distance_only_field_has_no_target(self):
        self._set_scale()
        self._select(self.pid_a)
        self.win.begin_location_fix()
        self.win.add_location_reference_field((50.0, 50.0), "well", 12.0, None)
        fx = self.proj.list_location_fixes(self.pid_a)[0]
        self.assertIsNone(fx["bearing_deg"])
        self.assertIsNone(fx["target_x"])

    def test_sheet_observation_computes_distance_bearing(self):
        self._set_scale(0.5)
        self._select(self.pid_a)
        self.win.begin_location_fix()
        self.win.add_location_reference_sheet((100.0, 100.0), (140.0, 100.0), "corner")
        fx = self.proj.list_location_fixes(self.pid_a)[0]
        self.assertEqual(fx["source"], "sheet")
        self.assertAlmostEqual(fx["distance_m"], 20.0)   # 40 px * 0.5
        self.assertAlmostEqual(fx["bearing_deg"], 90.0)  # East

    def test_cross_check_agrees_then_flags_disagreement(self):
        self._set_scale(0.5)
        self._select(self.pid_a)
        self.win.begin_location_fix()
        # Three field references that all imply the same target (140,100): agree.
        self.win.add_location_reference_field((100.0, 100.0), "a", 20.0, 90.0)
        self.win.add_location_reference_field((140.0, 60.0), "b", 20.0, 180.0)
        self.win.add_location_reference_field((180.0, 100.0), "c", 20.0, 270.0)
        self.assertIn("agree", self.win._location_crosscheck.text())
        # A fourth reference implying a far-off point: disagreement is surfaced.
        self.win.add_location_reference_field((100.0, 100.0), "d", 200.0, 90.0)
        self.assertIn("DISAGREE", self.win._location_crosscheck.text())


if __name__ == "__main__":
    unittest.main()
