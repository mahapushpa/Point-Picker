"""Window-level tests for M9 unit-profile display.

A parcel's live measurement must show SI plus the active local profile side by
side (never one replacing the other — the Scale-first verifiable baseline), and
with no profile selected must fall back to SI-only without crashing. Runs
offscreen; skipped if PySide6/Pillow are unavailable.
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
class MainUnitsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        img = tmp / "sheet.png"
        Image.new("RGB", (400, 400), (200, 200, 200)).save(img)
        proj = tmp / "proj"
        proj.mkdir()

        self.w = MainWindow()
        self.w._set_project(ProjectDB.create(str(proj)))
        self.w.load_path(str(img))

        # 1 px == 1 m, so a 100x100 px square == 10000 m² exactly.
        QInputDialog.getDouble = staticmethod(lambda *a, **k: (100.0, True))
        self.w.begin_scale_calibration()
        self.w.canvas._place_point(QPointF(0, 0))
        self.w.canvas._place_point(QPointF(100, 0))
        self.w.canvas.confirm_pick()

        self.w.new_parcel()
        for x, y in [(0, 0), (100, 0), (100, 100), (0, 100)]:
            self.w.canvas._place_point(QPointF(x, y))
        self.w.close_polygon()

    def tearDown(self):
        self.w.close()
        self._tmp.cleanup()

    def _select_unit(self, name_or_none):
        combo = self.w._unit_combo
        if name_or_none is None:
            combo.setCurrentIndex(0)  # "SI only"
            return
        for i in range(combo.count()):
            if combo.itemText(i).startswith(name_or_none):
                combo.setCurrentIndex(i)
                return
        raise AssertionError(f"unit {name_or_none!r} not in combo")

    def test_no_profile_shows_si_only(self):
        self._select_unit(None)
        text = self.w._measure_readout.text()
        self.assertIn("10000 m²", text)   # ~10000, SI baseline present
        self.assertNotIn("=", text)       # no second unit appended
        self.assertNotIn("acre", text)

    def test_active_profile_shown_beside_si(self):
        self._select_unit("acre")
        text = self.w._measure_readout.text()
        # SI baseline still there, acre shown alongside (10000 m² ~= 2.4711 acre).
        self.assertIn("10000 m²", text)
        self.assertIn("acre", text)
        self.assertIn("2.471", text)

    def test_user_profile_conversion_reflected(self):
        pid = self.w._project.create_unit_profile("Bigha — Jaipur", 2500.0)
        self.w._refresh_unit_combo()
        self._select_unit("Bigha — Jaipur")
        # 10000 m² / 2500 == 4 Bigha.
        self.assertEqual(self.w._project.get_source_unit_profile(self.w._source_id)["id"], pid)
        text = self.w._measure_readout.text()
        self.assertIn("Bigha — Jaipur", text)
        self.assertIn("4 Bigha", text)

    def test_switching_units_updates_live_without_changing_si(self):
        self._select_unit("hectare")
        text_ha = self.w._measure_readout.text()
        self.assertIn("hectare", text_ha)
        self.assertIn("10000 m²", text_ha)   # baseline unchanged
        self.assertIn("1 hectare", text_ha)  # 10000 m² == 1 ha
        self._select_unit(None)
        self.assertNotIn("hectare", self.w._measure_readout.text())

    def test_length_stays_si_only(self):
        # A local area profile must not fabricate a length conversion.
        self._select_unit("acre")
        text = self.w._measure_readout.text()
        # perimeter is 400 m; it must read in metres, not acres.
        self.assertIn("perim 400 m", text)

    def test_combo_disabled_without_source(self):
        w2 = MainWindow()
        try:
            self.assertFalse(w2._unit_combo.isEnabled())  # no project/source yet
        finally:
            w2.close()


if __name__ == "__main__":
    unittest.main()
