"""Milestone 12 — segment-length / boundary-description report.

Covers the pure bearing/compass geometry, per-edge enumeration from the topology
model, Scale-first blocking, user segment selection filtering, and the CSV / JSON
/ PDF writers. No PySide6 needed. PDF test skipped without PyMuPDF.
"""

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.project_db import ProjectDB
from src.core import geometry
from src.export import segment_report as S

try:
    from src.export.report import _import_fitz
    _import_fitz()
    _HAVE_FITZ = True
except Exception:
    _HAVE_FITZ = False

# A 100x100 axis-aligned square traced clockwise from the top-left, image y down.
# Edges: 0 top (V1->V2, East), 1 right (V2->V3, South), 2 bottom (V3->V4, West),
#        3 closing left (V4->V1, North).  0.5 m/px -> each side 50 m.
SQUARE = [(0, 0), (100, 0), (100, 100), (0, 100)]
MPP = 0.5


class BearingGeometryTests(unittest.TestCase):
    def test_cardinal_bearings_north_up_clockwise(self):
        self.assertAlmostEqual(geometry.bearing_deg((0, 0), (0, -10)), 0.0)     # up = N
        self.assertAlmostEqual(geometry.bearing_deg((0, 0), (10, 0)), 90.0)     # right = E
        self.assertAlmostEqual(geometry.bearing_deg((0, 0), (0, 10)), 180.0)    # down = S
        self.assertAlmostEqual(geometry.bearing_deg((0, 0), (-10, 0)), 270.0)   # left = W

    def test_compass_labels(self):
        self.assertEqual(geometry.compass_label(0), "North")
        self.assertEqual(geometry.compass_label(90), "East")
        self.assertEqual(geometry.compass_label(45), "North-east")
        self.assertEqual(geometry.compass_label(200), "South")
        self.assertEqual(geometry.compass_label(359), "North")   # wraps to nearest

    def test_degenerate_edge_is_zero(self):
        self.assertEqual(geometry.bearing_deg((5, 5), (5, 5)), 0.0)


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = ProjectDB.create(str(Path(self._tmp.name) / "proj"), name="Demo")
        sheet = Path(self._tmp.name) / "sheet.png"
        sheet.write_bytes(b"x")
        self.sid = self.p.import_source(sheet, "image")

    def tearDown(self):
        self.p.close()
        self._tmp.cleanup()

    def _parcel(self, *, scale=MPP, owner="Ramesh", points=SQUARE, closed=True):
        pid = self.p.create_parcel(self.sid, owner=owner)
        if scale is not None:
            self.p.set_source_scale(self.sid, scale, method="two-point", note="ok")
        self.p.save_parcel_polygon(pid, points, closed=closed, metres_per_pixel=scale)
        return pid


class EdgeListingTests(_Fixture):
    def test_closed_square_has_four_edges_with_lengths_and_bearings(self):
        pid = self._parcel()
        edges, ctx = S.list_parcel_edges(self.p, pid)
        self.assertEqual(len(edges), 4)               # incl. the closing edge
        self.assertTrue(ctx["has_scale"])
        self.assertEqual([e.compass for e in edges],
                         ["East", "South", "West", "North"])
        for e in edges:
            self.assertAlmostEqual(e.length_m, 50.0, places=6)
            self.assertAlmostEqual(e.length_ft, 50.0 / 0.3048, places=4)
        self.assertEqual(edges[0].vertex_a_label, "V1")
        self.assertEqual(edges[0].vertex_b_label, "V2")
        self.assertEqual(edges[3].vertex_b_label, "V1")   # closing edge wraps

    def test_open_polyline_has_no_closing_edge(self):
        pid = self._parcel(closed=False)
        edges, _ctx = S.list_parcel_edges(self.p, pid)
        self.assertEqual(len(edges), 3)               # no closing edge when open

    def test_no_scale_edges_have_no_length(self):
        pid = self._parcel(scale=None)
        edges, ctx = S.list_parcel_edges(self.p, pid)
        self.assertFalse(ctx["has_scale"])
        self.assertTrue(all(e.length_m is None for e in edges))
        # Bearings are still available without scale (closed square -> 4 edges).
        self.assertEqual([e.compass for e in edges], ["East", "South", "West", "North"])


class BuildReportTests(_Fixture):
    def test_selection_filters_included_segments(self):
        pid = self._parcel()
        rep = S.build_segment_report(self.p, pid, [0, 1], {0: "Road", 1: "Sita's field"})
        self.assertEqual(rep.segment_count, 2)
        self.assertEqual([r.seq for r in rep.rows], [1, 2])
        self.assertEqual(rep.rows[0].neighbour, "Road")
        self.assertEqual(rep.rows[1].neighbour, "Sita's field")
        self.assertAlmostEqual(rep.total_length_m, 100.0, places=6)   # two 50 m sides

    def test_selection_order_is_preserved(self):
        pid = self._parcel()
        rep = S.build_segment_report(self.p, pid, [2, 3])
        self.assertEqual([r.edge_index for r in rep.rows], [2, 3])
        self.assertEqual([r.compass for r in rep.rows], ["West", "North"])

    def test_no_scale_parcel_is_blocked(self):
        pid = self._parcel(scale=None)
        with self.assertRaises(S.SegmentReportError):
            S.build_segment_report(self.p, pid, [0, 1])

    def test_empty_selection_rejected(self):
        pid = self._parcel()
        with self.assertRaises(S.SegmentReportError):
            S.build_segment_report(self.p, pid, [])

    def test_unknown_edge_index_rejected(self):
        pid = self._parcel()
        with self.assertRaises(S.SegmentReportError):
            S.build_segment_report(self.p, pid, [99])


class SerialisationTests(_Fixture):
    def _report(self):
        pid = self._parcel()
        return S.build_segment_report(self.p, pid, [0, 1], {0: "Road"})

    def test_csv_structure_with_total(self):
        rep = self._report()
        out = Path(self._tmp.name) / "seg.csv"
        S.write_csv(rep, out)
        with open(out, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        header = rows[0]
        self.assertEqual(header[:4], ["Seq", "From", "To", "Length (m)"])
        self.assertIn("Direction", header)
        self.assertIn("Neighbour", header)
        total = next(r for r in rows if len(r) > 2 and r[2] == "TOTAL")
        self.assertEqual(float(total[3]), 100.0)
        note = next(r for r in rows if r and r[0] == "Bearing note")
        self.assertEqual(note[1], S.BEARING_CAVEAT)
        # Traditional description present (edge 0 = top side = East, neighbour Road).
        desc = rows[1][header.index("Description")]
        self.assertTrue(desc.startswith("East: bounded by Road"))

    def test_json_includes_rows_and_description(self):
        rep = self._report()
        out = Path(self._tmp.name) / "seg.json"
        S.write_json(rep, out)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["kind"], S.REPORT_KIND)
        self.assertEqual(len(data["rows"]), 2)
        self.assertIn("description", data["rows"][0])
        self.assertAlmostEqual(data["total_length_m"], 100.0, places=4)

    @unittest.skipUnless(_HAVE_FITZ, "PyMuPDF not available")
    def test_pdf_smoke(self):
        rep = self._report()
        out = Path(self._tmp.name) / "seg.pdf"
        S.write_pdf(rep, out)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 400)
        self.assertEqual(out.read_bytes()[:5], b"%PDF-")

    def test_bearing_caveat_in_report_and_csv_json(self):
        rep = self._report()
        self.assertEqual(rep.bearing_note, S.BEARING_CAVEAT)
        self.assertIn("assumed north-up", rep.bearing_note)
        self.assertIn("Location-fixing (M16)", rep.bearing_note)

        csv_out = Path(self._tmp.name) / "seg.csv"
        S.write_csv(rep, csv_out)
        self.assertIn(S.BEARING_CAVEAT, csv_out.read_text(encoding="utf-8"))

        json_out = Path(self._tmp.name) / "seg.json"
        S.write_json(rep, json_out)
        data = json.loads(json_out.read_text(encoding="utf-8"))
        self.assertEqual(data["bearing_note"], S.BEARING_CAVEAT)

    @unittest.skipUnless(_HAVE_FITZ, "PyMuPDF not available")
    def test_bearing_caveat_in_pdf_text(self):
        import pymupdf as fitz
        rep = self._report()
        out = Path(self._tmp.name) / "seg.pdf"
        S.write_pdf(rep, out)
        doc = fitz.open(str(out))
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        # The caveat renders in the base font (ASCII); normalise wrap whitespace.
        flat = " ".join(text.split())
        self.assertIn("assumed north-up", flat)
        self.assertIn("Location-fixing (M16)", flat)


class OrchestratorTests(_Fixture):
    def test_export_writes_named_files_and_blocks_without_scale(self):
        pid = self._parcel()
        rep, paths = S.export_segment_report(
            self.p, pid, self.p.exports_dir, edge_indices=[0, 1, 2],
            neighbours={0: "Road"}, formats=["csv", "json"])
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(p.exists() for p in paths))
        self.assertTrue(all(p.name.startswith("boundary-description_") for p in paths))

        # A parcel on a DIFFERENT, unscaled source is blocked (scale is per-source).
        other = Path(self._tmp.name) / "sheet2.png"
        other.write_bytes(b"y")
        sid2 = self.p.import_source(other, "image")
        nopid = self.p.create_parcel(sid2, owner="NoScale")
        self.p.save_parcel_polygon(nopid, SQUARE, closed=True)   # no scale on sid2
        with self.assertRaises(S.SegmentReportError):
            S.export_segment_report(self.p, nopid, self.p.exports_dir,
                                    edge_indices=[0], formats=["csv"])

    def test_regenerating_never_overwrites(self):
        pid = self._parcel()
        S.export_segment_report(self.p, pid, self.p.exports_dir,
                                edge_indices=[0], formats=["csv"])
        S.export_segment_report(self.p, pid, self.p.exports_dir,
                                edge_indices=[0], formats=["csv"])
        files = list(self.p.exports_dir.glob("boundary-description_*.csv"))
        self.assertEqual(len(files), 2)


if __name__ == "__main__":
    unittest.main()
