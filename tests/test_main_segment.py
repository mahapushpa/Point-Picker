"""Window-level test for the M12 boundary-description flow.

Drives MainWindow offscreen: scale-first blocking, the live side table filling
from a canvas edge selection, inline neighbour editing feeding the export, and
generating a report into exports/. Skipped without PySide6.
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
    from PySide6.QtCore import Qt
    from src.core.project_db import ProjectDB
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


SQUARE = [(0, 0), (100, 0), (100, 100), (0, 100)]


@unittest.skipUnless(_HAVE_QT and _HAVE_PIL, "PySide6 / Pillow not available")
class SegmentFlowTests(unittest.TestCase):
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

    def tearDown(self):
        self.win.close()
        try:
            self.proj.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _make_parcel(self, *, scale=0.5):
        if scale is not None:
            self.proj.set_source_scale(self.sid, scale, method="two-point", note="ok")
        pid = self.proj.create_parcel(self.sid, owner="Ramesh")
        self.proj.save_parcel_polygon(pid, SQUARE, closed=True, metres_per_pixel=scale)
        self.win._parcels = self.proj.list_parcels(self.sid)
        self.win._set_active_parcel(pid)
        return pid

    def test_no_scale_blocks_segment_mode(self):
        self._make_parcel(scale=None)
        self.win.begin_segment_description()
        self.assertFalse(self.win._segment_action.isChecked())
        self.assertFalse(self.win.canvas.is_segment_selecting())

    def test_selection_fills_table_with_length_and_bearing(self):
        self._make_parcel()
        self.win.begin_segment_description()
        self.assertTrue(self.win.canvas.is_segment_selecting())
        self.win.canvas.set_segment_selection([0, 1])   # top + right edges

        table = self.win._segment_table
        self.assertEqual(table.rowCount(), 2)
        # Row 0 = edge 0 (top side, East, 50 m).
        self.assertEqual(table.item(0, 1).text(), "V1")
        self.assertEqual(table.item(0, 2).text(), "V2")
        self.assertEqual(table.item(0, 3).text(), "50.00 m")
        self.assertIn("East", table.item(0, 4).text())
        self.assertIn("100.00 m", self.win._segment_total_label.text())

    def test_inline_neighbour_edit_feeds_export(self):
        pid = self._make_parcel()
        self.win.begin_segment_description()
        self.win.canvas.set_segment_selection([0, 1])
        # Type a neighbour on the first row (the editable Neighbour column).
        col = self.win._SEG_NEIGHBOUR_COL
        self.win._segment_table.item(0, col).setText("Village road")
        self.assertEqual(self.win._segment_neighbours.get(0), "Village road")

        self.win.generate_segment_report(formats=["csv"])
        files = list(self.proj.exports_dir.glob("boundary-description_*.csv"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("bounded by Village road", text)

    def test_generate_without_selection_is_a_no_op(self):
        self._make_parcel()
        self.win.begin_segment_description()
        self.win.generate_segment_report(formats=["csv"])
        self.assertEqual(list(self.proj.exports_dir.glob("boundary-description_*")), [])

    def test_hover_readout_shows_edge_length(self):
        self._make_parcel()
        self.win.begin_segment_description()
        self.win._on_edge_hovered(0)
        self.assertIn("50.00 m", self.win._status.text())


if __name__ == "__main__":
    unittest.main()
