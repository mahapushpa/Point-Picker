"""Window-level tests for M15 DXF support in the UI.

A DXF flows through the same load path as any other source, so the existing
scale/tracing/vertex machinery works unmodified; the DXF-header scale is offered
and cross-checked exactly like the PDF-metadata one, never auto-applied. Skipped
without PySide6 / ezdxf.
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
    import ezdxf
    from src.core.project_db import ProjectDB
    from src.core.geometry import measure_polygon
    from src.core.scale import compute_two_point_scale, METHOD_DXF_HEADER
    from src.ui.main_window import MainWindow
    _HAVE = True
except Exception:  # pragma: no cover
    _HAVE = False

_app = None


def setUpModule():
    global _app
    if _HAVE:
        _app = QApplication.instance() or QApplication([])
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)


def _answer(button):
    QMessageBox.question = staticmethod(lambda *a, **k: button)


@unittest.skipUnless(_HAVE, "PySide6 / ezdxf not available")
class DxfUiTests(unittest.TestCase):
    def _make_dxf(self, insunits=6, side=10.0, add_circle=False):
        d = Path(self._tmp.name) / "plan.dxf"
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = insunits
        msp = doc.modelspace()
        for a, b in [((0, 0), (side, 0)), ((side, 0), (side, side)),
                     ((side, side), (0, side)), ((0, side), (0, 0))]:
            msp.add_line(a, b)
        if add_circle:
            msp.add_circle((side / 2, side / 2), side / 5)
        doc.saveas(str(d))
        return d

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = ProjectDB.create(str(Path(self._tmp.name) / "proj"), name="Demo")
        self.win = MainWindow()
        self.win._set_project(self.proj)

    def tearDown(self):
        self.win.close()
        try:
            self.proj.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _load(self, **kw):
        self.win.load_path(str(self._make_dxf(**kw)))

    # -- source loads and is a normal raster source -------------------------

    def test_dxf_loads_as_source_with_image_and_right_action_states(self):
        self._load()
        self.assertTrue(self.win.canvas.has_image())
        self.assertIsNotNone(self.win._source_id)
        self.assertEqual(self.proj.get_source(self.win._source_id)["file_type"], "dxf")
        self.assertTrue(self.win._dxf_scale_action.isEnabled())
        self.assertFalse(self.win._pdf_scale_action.isEnabled())

    def test_skipped_entities_flagged_in_status(self):
        self._load(add_circle=True)
        self.assertIn("CIRCLE", self.win._status.text())

    # -- header-scale offer (method 1) --------------------------------------

    def test_offered_not_auto_applied_when_declined(self):
        self._load(insunits=6)
        self.assertIsNone(self.proj.get_source_scale(self.win._source_id))
        _answer(QMessageBox.StandardButton.No)
        self.win.propose_dxf_header_scale()
        self.assertIsNone(self.win._scale)
        self.assertIsNone(self.proj.get_source_scale(self.win._source_id))

    def test_applied_on_accept_with_correct_value(self):
        self._load(insunits=6, side=10.0)
        upp = self.win._dxf_info.units_per_pixel
        _answer(QMessageBox.StandardButton.Yes)
        self.win.propose_dxf_header_scale()
        row = self.proj.get_source_scale(self.win._source_id)
        self.assertEqual(row["method"], METHOD_DXF_HEADER)
        self.assertAlmostEqual(row["metres_per_pixel"], upp)   # metres: 1 m/unit
        self.assertEqual(self.win._scale.method, METHOD_DXF_HEADER)

    def test_unitless_dxf_is_not_offered(self):
        self._load(insunits=0)     # unitless header
        _answer(QMessageBox.StandardButton.Yes)   # even if the user would accept...
        self.win.propose_dxf_header_scale()        # ...there's no usable unit
        self.assertIsNone(self.win._scale)
        self.assertIsNone(self.proj.get_source_scale(self.win._source_id))

    # -- cross-check against a manual scale (method 4) ----------------------

    def test_cross_check_keeps_manual_when_declined(self):
        self._load(insunits=6)
        self.win._scale = compute_two_point_scale((0, 0), (100, 0), 50.0)  # 0.5 m/px
        self.win._persist_scale()
        _answer(QMessageBox.StandardButton.No)
        self.win.propose_dxf_header_scale()
        self.assertEqual(self.proj.get_source_scale(self.win._source_id)["method"], "two-point")
        self.assertAlmostEqual(self.win._scale.metres_per_pixel, 0.5)

    def test_cross_check_replaces_manual_on_accept(self):
        self._load(insunits=6)
        upp = self.win._dxf_info.units_per_pixel
        self.win._scale = compute_two_point_scale((0, 0), (100, 0), 50.0)
        self.win._persist_scale()
        _answer(QMessageBox.StandardButton.Yes)
        self.win.propose_dxf_header_scale()
        row = self.proj.get_source_scale(self.win._source_id)
        self.assertEqual(row["method"], METHOD_DXF_HEADER)
        self.assertAlmostEqual(row["metres_per_pixel"], upp)

    # -- downstream machinery is unchanged on a DXF source ------------------

    def test_scale_and_measurement_pipeline_works_on_dxf(self):
        self._load(insunits=6)
        _answer(QMessageBox.StandardButton.Yes)
        self.win.propose_dxf_header_scale()
        mpp = self.win._mpp()
        self.assertIsNotNone(mpp)

        sid = self.win._source_id
        pid = self.proj.create_parcel(sid, owner="Ramesh")
        square_px = [(0, 0), (200, 0), (200, 200), (0, 200)]
        self.proj.save_parcel_polygon(pid, square_px, closed=True, metres_per_pixel=mpp)

        # Vertex SI coordinates are populated (the shared-vertex machinery ran).
        verts = self.proj.list_vertices(sid)
        self.assertTrue(verts)
        self.assertTrue(all(v["local_x"] is not None for v in verts))

        # And geometry measures a positive real-world area at the DXF-derived scale.
        poly = self.proj.get_parcel_polygon(pid)
        m = measure_polygon(poly, mpp, closed=True)
        self.assertTrue(m.has_scale)
        self.assertAlmostEqual(m.area_sq_m, (200 * mpp) ** 2, places=6)


if __name__ == "__main__":
    unittest.main()
