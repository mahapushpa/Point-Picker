"""Milestone 11 — owner-wise summary report engine.

Covers the pure report model, the per-owner CSV / JSON / PDF writers, the
one-file-per-owner orchestrator (naming + never-overwrite), and the per-parcel
boundary-image crop (embed vs separate-PNG fallback). No PySide6 needed — the
report engine is pure logic. PDF/crop tests are skipped if PyMuPDF / Pillow are
unavailable.

Correctness the brief calls out:
  * scale-first — no-scale parcels appear but carry no area, excluded from totals;
  * SI canonical + derived hectare/acre, local profile alongside;
  * grouping by owner (blank -> "(no owner)" bucket, sorted last).
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
from src.core import units
from src.export import report as R

try:
    from PIL import Image, ImageDraw
    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False

try:
    R._import_fitz()
    _HAVE_FITZ = True
except Exception:
    _HAVE_FITZ = False

# A 100x100 px square. With 0.5 m/px that is 50x50 m = 2500 m².
SQUARE = [(0, 0), (100, 0), (100, 100), (0, 100)]
MPP = 0.5
SQUARE_AREA_M2 = 2500.0
SQUARE_PERIM_M = 200.0


def _group_by_owner(report):
    return {g.owner: g for g in report.groups}


class _ProjectFixture(unittest.TestCase):
    """Uses a placeholder (non-image) source file — fine for everything except
    the crop tests, which have their own real-image fixture."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = ProjectDB.create(str(Path(self._tmp.name) / "proj"), name="Demo")
        self.sheet = Path(self._tmp.name) / "sheet.png"
        self.sheet.write_bytes(b"x")
        self.sid = self.p.import_source(self.sheet, "image")

    def tearDown(self):
        self.p.close()
        self._tmp.cleanup()

    def _traced_parcel(self, owner, points=SQUARE, closed=True, mpp=MPP):
        pid = self.p.create_parcel(self.sid, owner=owner)
        if mpp is not None:
            self.p.set_source_scale(self.sid, mpp, method="two-point",
                                    note="grid+page agree ~1%")
        self.p.save_parcel_polygon(pid, points, closed=closed, metres_per_pixel=mpp)
        return pid

    def _exports(self, pattern):
        return sorted(self.p.exports_dir.glob(pattern))


class BuildOwnerReportTests(_ProjectFixture):
    def test_area_matches_scaled_geometry(self):
        self._traced_parcel("Ramesh")
        rep = R.build_owner_report(self.p)
        self.assertEqual(rep.parcel_count, 1)
        parcel = rep.groups[0].parcels[0]
        self.assertAlmostEqual(parcel.area_sq_m, SQUARE_AREA_M2, places=6)
        self.assertAlmostEqual(parcel.perimeter_m, SQUARE_PERIM_M, places=6)
        self.assertAlmostEqual(parcel.area_hectare, 0.25, places=6)
        self.assertAlmostEqual(parcel.area_acre,
                               SQUARE_AREA_M2 / units.BUILTIN_AREA_UNITS["acre"], places=9)

    def test_groups_by_owner_and_totals(self):
        self._traced_parcel("Ramesh")
        self._traced_parcel("Ramesh")
        self._traced_parcel("Sita")
        rep = R.build_owner_report(self.p)
        by_owner = _group_by_owner(rep)
        self.assertAlmostEqual(by_owner["Ramesh"].total_area_sq_m, 2 * SQUARE_AREA_M2, places=6)
        self.assertEqual(by_owner["Ramesh"].parcel_count, 2)
        self.assertAlmostEqual(by_owner["Sita"].total_area_sq_m, SQUARE_AREA_M2, places=6)
        self.assertAlmostEqual(rep.grand_total_area_sq_m, 3 * SQUARE_AREA_M2, places=6)
        self.assertEqual(rep.owner_count, 2)

    def test_owner_sorting_no_owner_last(self):
        self._traced_parcel("Zia")
        self._traced_parcel("Amit")
        self._traced_parcel(None)
        rep = R.build_owner_report(self.p)
        self.assertEqual([g.display_owner for g in rep.groups],
                         ["Amit", "Zia", R.NO_OWNER_LABEL])

    def test_blank_owner_is_treated_as_no_owner(self):
        self._traced_parcel("   ")
        rep = R.build_owner_report(self.p)
        self.assertEqual(rep.groups[0].owner, None)
        self.assertEqual(rep.owner_count, 0)

    def test_no_scale_parcel_has_no_area_and_is_excluded_from_total(self):
        self._traced_parcel("Ramesh")
        other = Path(self._tmp.name) / "sheet2.png"
        other.write_bytes(b"y")
        sid2 = self.p.import_source(other, "image")
        pid2 = self.p.create_parcel(sid2, owner="Ramesh")
        self.p.save_parcel_polygon(pid2, SQUARE, closed=True)   # no scale on sid2
        rep = R.build_owner_report(self.p)
        group = rep.groups[0]
        areas = {pr.parcel_id: pr.area_sq_m for pr in group.parcels}
        self.assertIsNone(areas[pid2])
        self.assertEqual(group.missing_scale_count, 1)
        self.assertEqual(group.scaled_count, 1)
        self.assertAlmostEqual(group.total_area_sq_m, SQUARE_AREA_M2, places=6)
        self.assertEqual(rep.missing_scale_count, 1)

    def test_carries_scale_method_note_and_source_reference(self):
        self._traced_parcel("Ramesh")
        rep = R.build_owner_report(self.p)
        parcel = rep.groups[0].parcels[0]
        self.assertEqual(parcel.scale_method, "two-point")
        self.assertIn("grid+page", parcel.scale_note)
        self.assertEqual(parcel.source.original_name, "sheet.png")
        self.assertEqual(rep.scale_methods, ["two-point"])

    def test_identification_fields_are_included(self):
        pid = self._traced_parcel("Ramesh")
        self.p.set_parcel_fields(pid, [("Khasra number", "K-123"), ("Village", "Rampur")])
        rep = R.build_owner_report(self.p)
        parcel = rep.groups[0].parcels[0]
        self.assertEqual(dict(parcel.fields)["Khasra number"], "K-123")

    def test_local_unit_shown_alongside_si(self):
        self._traced_parcel("Ramesh")
        bigha = self.p.create_unit_profile("Bigha — Test", 2500.0)
        self.p.set_source_unit_profile(self.sid, bigha)
        rep = R.build_owner_report(self.p)
        parcel = rep.groups[0].parcels[0]
        self.assertEqual(parcel.local_unit, "Bigha — Test")
        self.assertAlmostEqual(parcel.local_area, 1.0, places=6)
        self.assertEqual(rep.groups[0].total_local_unit, "Bigha — Test")
        self.assertAlmostEqual(rep.groups[0].total_local_area, 1.0, places=6)

    def test_parcel_ids_filter_scopes_the_report(self):
        keep = self._traced_parcel("Ramesh")
        self._traced_parcel("Sita")
        rep = R.build_owner_report(self.p, parcel_ids=[keep])
        self.assertEqual(rep.parcel_count, 1)
        self.assertEqual(rep.groups[0].owner, "Ramesh")

    def test_incomplete_boundary_has_no_area(self):
        pid = self.p.create_parcel(self.sid, owner="Ramesh")
        self.p.set_source_scale(self.sid, MPP, method="two-point")
        self.p.save_parcel_polygon(pid, [(0, 0), (10, 0)], closed=False)
        rep = R.build_owner_report(self.p)
        parcel = rep.groups[0].parcels[0]
        self.assertIsNone(parcel.area_sq_m)
        self.assertIsNotNone(parcel.perimeter_m)


class PerOwnerWriterTests(_ProjectFixture):
    """The writers operate on a single OwnerGroup (one file per owner)."""

    def _one_group(self, owner="Ramesh"):
        rep = R.build_owner_report(self.p)
        group = next(g for g in rep.groups if g.owner == owner)
        return rep, group

    def test_csv_row_per_parcel_plus_owner_total_no_grand_total(self):
        self._traced_parcel("Ramesh")
        self._traced_parcel("Ramesh")
        rep, group = self._one_group()
        out = Path(self._tmp.name) / "r.csv"
        R.write_csv(rep, group, out)
        with open(out, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        header = rows[0]
        self.assertEqual(header[0], "Owner")
        self.assertIn("Area (m²)", header)
        self.assertIn("Boundary image", header)
        # 2 parcels + 1 owner TOTAL = 3 data rows; no GRAND TOTAL in a per-owner file.
        self.assertEqual(len(rows), 1 + 3)
        self.assertFalse(any(r[1].startswith("GRAND TOTAL") for r in rows))
        total = next(r for r in rows if r[1].startswith("TOTAL"))
        self.assertEqual(float(total[header.index("Area (m²)")]), round(2 * SQUARE_AREA_M2, 2))

    def test_csv_identification_labels_become_columns(self):
        pid = self._traced_parcel("Ramesh")
        self.p.set_parcel_fields(pid, [("Khasra number", "K-9")])
        rep, group = self._one_group()
        out = Path(self._tmp.name) / "r.csv"
        R.write_csv(rep, group, out)
        with open(out, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        header = rows[0]
        self.assertIn("Khasra number", header)
        self.assertEqual(rows[1][header.index("Khasra number")], "K-9")

    def test_csv_no_scale_leaves_area_blank(self):
        pid = self.p.create_parcel(self.sid, owner="Ramesh")
        self.p.save_parcel_polygon(pid, SQUARE, closed=True)   # no scale
        rep, group = self._one_group()
        out = Path(self._tmp.name) / "r.csv"
        R.write_csv(rep, group, out)
        with open(out, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        header = rows[0]
        self.assertEqual(rows[1][header.index("Area (m²)")], "")
        self.assertEqual(rows[1][header.index("Scale method")], "(no scale)")

    def test_json_is_single_owner_structure(self):
        self._traced_parcel("Ramesh")
        rep, group = self._one_group()
        out = Path(self._tmp.name) / "r.json"
        R.write_json(rep, group, out)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["kind"], R.REPORT_KIND)
        self.assertEqual(data["owner"], "Ramesh")
        self.assertNotIn("groups", data)                 # one owner, not the whole report
        self.assertAlmostEqual(data["group"]["parcels"][0]["area_sq_m"],
                               SQUARE_AREA_M2, places=2)

    @unittest.skipUnless(_HAVE_FITZ, "PyMuPDF not available")
    def test_pdf_is_written_and_nonempty(self):
        self._traced_parcel("Ramesh")
        rep, group = self._one_group()
        out = Path(self._tmp.name) / "r.pdf"
        R.write_pdf(rep, group, out)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 500)
        self.assertEqual(out.read_bytes()[:5], b"%PDF-")

    @unittest.skipUnless(_HAVE_FITZ, "PyMuPDF not available")
    def test_devanagari_text_round_trips_into_pdf(self):
        # A Devanagari owner/field value must render as real text (embedded Noto
        # font), not blank/tofu — verified by extracting text back out. Mixed with
        # Latin, both scripts must survive in the same document.
        owner = "रमेश कुमार"          # "Ramesh Kumar" in Devanagari
        pid = self._traced_parcel(owner)
        self.p.set_parcel_fields(pid, [("गाँव", "ढोलाखेड़ा"), ("Khasra", "123")])
        rep, group = self._one_group(owner)
        out = Path(self._tmp.name) / "deva.pdf"
        R.write_pdf(rep, group, out)

        fitz = R._import_fitz()
        with fitz.open(str(out)) as doc:
            text = "\n".join(p.get_text("text") for p in doc)
        self.assertIn("रमेश कुमार", text)      # Devanagari owner survived
        self.assertIn("ढोलाखेड़ा", text)        # Devanagari field value survived
        self.assertIn("Khasra", text)          # Latin still fine in the same PDF
        self.assertNotIn("�", text)       # no replacement char / tofu


class OwnerSlugTests(unittest.TestCase):
    def test_blank_bucket_and_sanitising(self):
        self.assertEqual(R._owner_slug("Ramesh Kumar"), "ramesh-kumar")
        self.assertEqual(R._owner_slug(R.NO_OWNER_LABEL), "no-owner")
        self.assertEqual(R._owner_slug("A/B"), "a-b")
        self.assertEqual(R._owner_slug("!!!"), "owner")   # nothing left -> fallback


class OrchestratorTests(_ProjectFixture):
    """export_owner_reports: one file per owner per format, safely named."""

    @unittest.skipUnless(_HAVE_FITZ, "PyMuPDF not available")
    def test_one_file_per_owner_for_each_format(self):
        self._traced_parcel("Ramesh")
        self._traced_parcel("Sita")
        self._traced_parcel(None)          # blank-owner bucket
        report, report_paths, _imgs = R.export_owner_reports(
            self.p, self.p.exports_dir, formats=["pdf", "csv"])
        # 3 owner groups × 2 formats = 6 files.
        self.assertEqual(len(report_paths), 6)
        self.assertEqual(len(self._exports("owner-summary_*.pdf")), 3)
        self.assertEqual(len(self._exports("owner-summary_*.csv")), 3)
        # One file names the blank-owner bucket, unambiguously.
        self.assertEqual(len(list(self.p.exports_dir.glob("owner-summary_*_no-owner_*.csv"))), 1)
        # Each owner's name appears in its own filename.
        self.assertTrue(list(self.p.exports_dir.glob("*_ramesh_*.csv")))
        self.assertTrue(list(self.p.exports_dir.glob("*_sita_*.csv")))

    def test_slug_collision_still_distinct_files(self):
        # Two different owners that slugify identically must not collide.
        self._traced_parcel("A/B")
        self._traced_parcel("A-B")
        _rep, report_paths, _imgs = R.export_owner_reports(
            self.p, self.p.exports_dir, formats=["csv"])
        self.assertEqual(len(report_paths), 2)
        self.assertEqual(len(set(report_paths)), 2)                 # distinct paths
        self.assertEqual(len(self._exports("owner-summary_*_a-b_*.csv")), 2)

    def test_regenerating_never_overwrites(self):
        self._traced_parcel("Ramesh")
        R.export_owner_reports(self.p, self.p.exports_dir, formats=["csv"])
        R.export_owner_reports(self.p, self.p.exports_dir, formats=["csv"])
        self.assertEqual(len(self._exports("owner-summary_*_ramesh_*.csv")), 2)

    def test_unknown_format_rejected(self):
        self._traced_parcel("Ramesh")
        with self.assertRaises(ValueError):
            R.export_owner_reports(self.p, self.p.exports_dir, formats=["txt"])


class UniqueExportPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_path_is_timestamped_in_the_folder(self):
        from datetime import datetime
        ts = datetime(2026, 8, 19, 14, 30, 5)
        path = R.unique_export_path(self.dir, "owner-summary_01_ramesh", "csv", timestamp=ts)
        self.assertEqual(path.name, "owner-summary_01_ramesh_20260819-143005.csv")
        self.assertEqual(path.parent, self.dir)

    def test_collision_gets_an_index_never_overwrites(self):
        from datetime import datetime
        ts = datetime(2026, 8, 19, 14, 30, 5)
        first = R.unique_export_path(self.dir, "s", "csv", timestamp=ts)
        first.write_text("x", encoding="utf-8")
        second = R.unique_export_path(self.dir, "s", "csv", timestamp=ts)
        self.assertNotEqual(first, second)
        self.assertEqual(second.name, "s_20260819-143005_2.csv")


@unittest.skipUnless(_HAVE_PIL, "Pillow not available")
class BoundaryCropTests(unittest.TestCase):
    """Item 3: cropped boundary image per parcel; separate-PNG fallback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.p = ProjectDB.create(str(self.root / "proj"), name="Demo")
        # A real 300x200 raster with a coloured block, so crops have content.
        self.img_path = self.root / "sheet.png"
        img = Image.new("RGB", (300, 200), (255, 255, 255))
        ImageDraw.Draw(img).rectangle([120, 40, 180, 120], fill=(0, 120, 200))
        img.save(self.img_path)
        self.sid = self.p.import_source(self.img_path, "image")
        self.p.set_source_scale(self.sid, 0.5, method="two-point")

    def tearDown(self):
        self.p.close()
        self._tmp.cleanup()

    def test_render_crop_captures_the_right_region_and_draws_boundary(self):
        from src.export import boundary_image as BI
        poly = [(50, 40), (90, 40), (90, 80), (50, 80)]   # bbox 40x40
        crop = BI.render_crop(self.img_path, poly)
        # bbox(40) + 2*min_pad(10) + 1 = 61 px each side (padding_frac*40 < min_pad).
        self.assertTrue(59 <= crop.width <= 63)
        self.assertTrue(59 <= crop.height <= 63)
        # The drawn boundary outline colour is present -> non-empty, correct region.
        self.assertIn(bytes((230, 60, 10)), crop.convert("RGB").tobytes())

    def test_multi_parcel_crops_embedded_when_reasonable(self):
        if not _HAVE_FITZ:
            self.skipTest("PyMuPDF not available")
        for poly in ([(50, 40), (90, 40), (90, 80), (50, 80)],
                     [(150, 60), (200, 60), (200, 110), (150, 110)]):
            pid = self.p.create_parcel(self.sid, owner="Ramesh")
            self.p.save_parcel_polygon(pid, poly, closed=True, metres_per_pixel=0.5)
        _rep, report_paths, image_paths = R.export_owner_reports(
            self.p, self.p.exports_dir, formats=["pdf"])
        self.assertEqual(len(report_paths), 1)          # one owner
        self.assertEqual(image_paths, [])               # both crops embedded, none external

    def test_many_parcels_one_source_decodes_source_once(self):
        # An owner with many parcels on the SAME sheet must decode that sheet
        # once for the whole report, not once per parcel (the pre-fix behaviour
        # re-ran open_raster for every parcel).
        if not _HAVE_FITZ:
            self.skipTest("PyMuPDF not available")
        from src.export import boundary_image as BI

        for i in range(30):
            # 30 small, distinct, embeddable boundaries across the 300x200 sheet.
            x = 10 + (i % 10) * 25
            y = 10 + (i // 10) * 55
            poly = [(x, y), (x + 18, y), (x + 18, y + 18), (x, y + 18)]
            pid = self.p.create_parcel(self.sid, owner="Ramesh")
            self.p.save_parcel_polygon(pid, poly, closed=True, metres_per_pixel=0.5)

        calls = {"n": 0}
        real_open = BI.open_raster

        def counting_open(path, **kwargs):
            calls["n"] += 1
            return real_open(path, **kwargs)

        BI.open_raster = counting_open
        try:
            R.export_owner_reports(self.p, self.p.exports_dir, formats=["pdf"])
        finally:
            BI.open_raster = real_open
        self.assertEqual(calls["n"], 1,
                         f"source decoded {calls['n']} times for 30 parcels on one sheet")

    def test_awkward_aspect_falls_back_to_separate_png_and_is_referenced(self):
        # A very wide, thin boundary -> extreme aspect -> external PNG.
        wide = [(10, 100), (290, 100), (290, 108), (10, 108)]
        pid = self.p.create_parcel(self.sid, owner="Ramesh")
        self.p.save_parcel_polygon(pid, wide, closed=True, metres_per_pixel=0.5)
        _rep, report_paths, image_paths = R.export_owner_reports(
            self.p, self.p.exports_dir, formats=["csv"])
        self.assertEqual(len(image_paths), 1)
        png = image_paths[0]
        self.assertTrue(png.exists() and png.stat().st_size > 0)
        self.assertEqual(png.suffix, ".png")
        # CSV references the PNG filename.
        csv_text = report_paths[0].read_text(encoding="utf-8")
        self.assertIn(png.name, csv_text)


if __name__ == "__main__":
    unittest.main()
