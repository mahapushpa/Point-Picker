"""Tests for core.project_db — schema, seeding, sources, and portability.

Standard-library unittest only, so these run with no third-party install:
    python -m unittest discover -s tests
(They also run under pytest if you prefer: `pytest tests`.)
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.project_db import (  # noqa: E402
    ProjectDB, ProjectError, SCHEMA_VERSION, BUILTIN_UNIT_PROFILES,
)


class ProjectDBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lmt_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_makes_folder_layout_and_schema(self):
        root = self.tmp / "proj"
        with ProjectDB.create(root, name="Test") as p:
            self.assertTrue(p.db_path.is_file())
            self.assertTrue(p.sources_dir.is_dir())
            self.assertTrue(p.exports_dir.is_dir())
            self.assertEqual(p.schema_version, SCHEMA_VERSION)
            expected = {"meta", "sources", "unit_profiles", "parcels",
                        "parcel_fields", "vertices", "parcel_vertices"}
            self.assertTrue(expected.issubset(set(p.table_names())))
            self.assertEqual(p.get_meta("project_name"), "Test")

    def test_builtin_units_seeded(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            names = {u["name"] for u in p.list_unit_profiles()}
            self.assertEqual(names, {n for n, _ in BUILTIN_UNIT_PROFILES})
            self.assertTrue(all(u["is_builtin"] for u in p.list_unit_profiles()))

    def test_create_rejects_existing_project(self):
        root = self.tmp / "proj"
        ProjectDB.create(root).close()
        with self.assertRaises(ProjectError):
            ProjectDB.create(root)

    def test_open_missing_project_raises(self):
        with self.assertRaises(ProjectError):
            ProjectDB.open(self.tmp / "nope")

    def test_open_wrong_schema_version_raises(self):
        root = self.tmp / "proj"
        ProjectDB.create(root).close()
        conn = sqlite3.connect(str(root / "project.db"))
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
        conn.close()
        with self.assertRaises(ProjectError):
            ProjectDB.open(root)

    def test_import_source_copies_file_and_stores_relative_path(self):
        root = self.tmp / "proj"
        sample = self.tmp / "sheet.png"
        sample.write_bytes(b"not-really-a-png")
        with ProjectDB.create(root) as p:
            sid = p.import_source(sample, "image")
            self.assertIsInstance(sid, int)
            (src,) = p.list_sources()
            self.assertEqual(src["relative_path"], "sources/sheet.png")
            self.assertTrue((root / "sources" / "sheet.png").is_file())
            self.assertTrue(p.resolve(src["relative_path"]).is_file())

    def test_import_source_infers_type_from_extension(self):
        root = self.tmp / "proj"
        for fname, expected in [("a.pdf", "pdf"), ("b.dxf", "dxf"), ("c.jpg", "image")]:
            f = self.tmp / fname
            f.write_bytes(b"x")
            with ProjectDB.open(root) if root.exists() else ProjectDB.create(root) as p:
                sid = p.import_source(f)
                got = [s for s in p.list_sources() if s["id"] == sid][0]
                self.assertEqual(got["file_type"], expected)

    def test_portability_copied_folder_opens_from_new_path(self):
        """The core promise: copy the whole folder to a different absolute path
        (as a pen drive would be) and it opens and resolves identically."""
        original = self.tmp / "original"
        sample = self.tmp / "sheet.png"
        sample.write_bytes(b"bytes")
        with ProjectDB.create(original, name="Portable") as p:
            p.import_source(sample, "image")
            p.set_meta("marker", "v1")

        copied = self.tmp / "elsewhere" / "moved-project"
        copied.parent.mkdir(parents=True)
        shutil.copytree(original, copied)

        with ProjectDB.open(copied) as p2:
            self.assertEqual(p2.get_meta("marker"), "v1")
            (src,) = p2.list_sources()
            self.assertEqual(src["relative_path"], "sources/sheet.png")
            resolved = p2.resolve(src["relative_path"])
            self.assertTrue(resolved.is_file())
            self.assertEqual(resolved.parent.parent, copied.resolve())

    def test_no_absolute_paths_stored_in_db(self):
        root = self.tmp / "proj"
        sample = self.tmp / "sheet.pdf"
        sample.write_bytes(b"x")
        with ProjectDB.create(root) as p:
            p.import_source(sample, "pdf")
        conn = sqlite3.connect(str(root / "project.db"))
        try:
            conn.row_factory = sqlite3.Row
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for t in tables:
                for row in conn.execute(f"SELECT * FROM {t}"):
                    for val in tuple(row):
                        if isinstance(val, str):
                            self.assertNotIn(":\\", val, f"drive path in {t}: {val!r}")
                            self.assertFalse(val.startswith("/"), f"abs path in {t}: {val!r}")
                            self.assertNotIn(str(root), val, f"root leaked into {t}: {val!r}")
        finally:
            conn.close()

    def test_add_custom_unit_profile(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            uid = p.add_unit_profile("Bigha - Jaipur", 2529.29)
            got = [u for u in p.list_unit_profiles() if u["id"] == uid][0]
            self.assertFalse(got["is_builtin"])
            self.assertAlmostEqual(got["sq_m_per_unit"], 2529.29)

    # -- scale persistence (Milestone 3) ------------------------------------

    def _source_id(self, p):
        sample = self.tmp / "sheet.png"
        if not sample.exists():
            sample.write_bytes(b"x")
        return p.import_source(sample, "image")

    def test_source_scale_round_trip(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            self.assertIsNone(p.get_source_scale(sid))
            p.set_source_scale(sid, 0.0421, method="two-point",
                               p1=(10.0, 20.0), p2=(110.0, 20.0),
                               real_distance_m=4.21, note="grid cross-check pending")
            got = p.get_source_scale(sid)
            self.assertAlmostEqual(got["metres_per_pixel"], 0.0421)
            self.assertEqual(got["method"], "two-point")
            self.assertAlmostEqual(got["p1x"], 10.0)
            self.assertAlmostEqual(got["p2x"], 110.0)
            self.assertAlmostEqual(got["real_distance_m"], 4.21)
            self.assertEqual(got["note"], "grid cross-check pending")
            self.assertTrue(got["updated_at"])

    def test_source_scale_persists_across_reopen(self):
        root = self.tmp / "proj"
        with ProjectDB.create(root) as p:
            sid = self._source_id(p)
            p.set_source_scale(sid, 0.25, p1=(0.0, 0.0), p2=(100.0, 0.0),
                               real_distance_m=25.0)
        with ProjectDB.open(root) as p2:
            got = p2.get_source_scale(sid)
            self.assertAlmostEqual(got["metres_per_pixel"], 0.25)

    def test_set_source_scale_overwrites(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            p.set_source_scale(sid, 0.5)
            p.set_source_scale(sid, 0.75)  # re-calibrate
            self.assertAlmostEqual(p.get_source_scale(sid)["metres_per_pixel"], 0.75)

    def test_clear_source_scale(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            p.set_source_scale(sid, 0.5)
            p.clear_source_scale(sid)
            self.assertIsNone(p.get_source_scale(sid))

    def test_set_scale_rejects_nonpositive(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            with self.assertRaises(ProjectError):
                p.set_source_scale(sid, 0.0)

    def test_set_scale_rejects_unknown_source(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            with self.assertRaises(ProjectError):
                p.set_source_scale(9999, 0.5)

    def test_scale_deleted_when_source_deleted(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            p.set_source_scale(sid, 0.5)
            p.conn.execute("DELETE FROM sources WHERE id = ?", (sid,))
            p.conn.commit()
            self.assertIsNone(p.get_source_scale(sid))  # ON DELETE CASCADE

    def test_v1_project_upgrades_additively_on_open(self):
        """A v1 project (no source_scales table) opens by additive upgrade to
        the current version, gaining the new table without losing data."""
        root = self.tmp / "legacy"
        with ProjectDB.create(root) as p:
            sid = self._source_id(p)
        # Downgrade the file to look like a v1 project.
        conn = sqlite3.connect(str(root / "project.db"))
        conn.execute("DROP TABLE source_scales")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        # Opening should migrate it up and let scale be stored.
        with ProjectDB.open(root) as p2:
            self.assertEqual(p2.schema_version, SCHEMA_VERSION)
            p2.set_source_scale(sid, 0.5)
            self.assertAlmostEqual(p2.get_source_scale(sid)["metres_per_pixel"], 0.5)

    # -- idempotent source registration (Milestone 4) -----------------------

    def test_import_or_get_source_is_idempotent(self):
        sample = self.tmp / "sheet.png"
        sample.write_bytes(b"img")
        with ProjectDB.create(self.tmp / "proj") as p:
            sid1, existed1 = p.import_or_get_source(sample, "image")
            self.assertFalse(existed1)
            sid2, existed2 = p.import_or_get_source(sample, "image")
            self.assertTrue(existed2)
            self.assertEqual(sid1, sid2)
            self.assertEqual(len(p.list_sources()), 1)

    # -- parcel polygon persistence (Milestone 5) ---------------------------

    def test_save_and_read_parcel_polygon(self):
        pts = [(0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0)]
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
            p.save_parcel_polygon(pid, pts, metres_per_pixel=0.5)
            self.assertEqual(p.get_parcel_polygon(pid), pts)  # order preserved
            stored = p.get_parcel_points(pid)
            self.assertEqual([s["seq"] for s in stored], [0, 1, 2, 3])
            # local (SI) coords populated from the scale: 100 px * 0.5 = 50 m
            self.assertAlmostEqual(stored[1]["local_x"], 50.0)
            self.assertAlmostEqual(stored[2]["local_y"], 25.0)

    def test_save_parcel_polygon_without_scale_leaves_local_null(self):
        pts = [(0.0, 0.0), (10.0, 0.0), (5.0, 8.0)]
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
            p.save_parcel_polygon(pid, pts)
            stored = p.get_parcel_points(pid)
            self.assertTrue(all(s["local_x"] is None and s["local_y"] is None for s in stored))

    def test_save_parcel_polygon_replaces_previous(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
            p.save_parcel_polygon(pid, [(0.0, 0.0), (100.0, 100.0), (200.0, 0.0)])
            p.save_parcel_polygon(pid, [(0.0, 0.0), (500.0, 0.0)])  # re-trace
            self.assertEqual(p.get_parcel_polygon(pid), [(0.0, 0.0), (500.0, 0.0)])

    def test_parcel_polygon_persists_across_reopen(self):
        root = self.tmp / "proj"
        pts = [(1.0, 2.0), (3.0, 4.0), (5.0, 1.0)]
        with ProjectDB.create(root) as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
            p.save_parcel_polygon(pid, pts, metres_per_pixel=0.1)
        with ProjectDB.open(root) as p2:
            self.assertEqual(p2.get_parcel_polygon(pid), pts)

    def test_empty_save_clears_points_keeps_parcel(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
            p.save_parcel_polygon(pid, [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)])
            p.save_parcel_polygon(pid, [])
            self.assertEqual(p.get_parcel_polygon(pid), [])
            self.assertIsNotNone(p.get_parcel(pid))  # parcel row remains

    def test_save_parcel_polygon_rejects_unknown_parcel(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            with self.assertRaises(ProjectError):
                p.save_parcel_polygon(9999, [(0.0, 0.0), (1.0, 0.0)])

    def test_create_parcel_rejects_unknown_source(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            with self.assertRaises(ProjectError):
                p.create_parcel(9999)

    # -- multiple parcels per source (Milestone 5) --------------------------

    def test_multiple_parcels_on_one_source_round_trip(self):
        root = self.tmp / "proj"
        a = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
        b = [(50.0, 50.0), (60.0, 50.0), (60.0, 60.0), (50.0, 60.0)]
        with ProjectDB.create(root) as p:
            sid = self._source_id(p)
            pa = p.create_parcel(sid, owner="Ramesh")
            pb = p.create_parcel(sid, owner="Suresh")
            p.save_parcel_polygon(pa, a, closed=True)
            p.save_parcel_polygon(pb, b, closed=False)
            self.assertEqual(len(p.list_parcels(sid)), 2)
        with ProjectDB.open(root) as p2:
            parcels = p2.list_parcels(sid)
            self.assertEqual([pc["id"] for pc in parcels], [pa, pb])
            self.assertEqual(p2.get_parcel_polygon(pa), a)
            self.assertEqual(p2.get_parcel_polygon(pb), b)
            self.assertTrue(p2.get_parcel_closed(pa))
            self.assertFalse(p2.get_parcel_closed(pb))
            self.assertEqual([pc["owner"] for pc in parcels], ["Ramesh", "Suresh"])
            self.assertEqual([pc["point_count"] for pc in parcels], [3, 4])

    def test_editing_one_parcel_does_not_corrupt_another(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            pa = p.create_parcel(sid)
            pb = p.create_parcel(sid)
            a = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]      # far from b: no sharing
            b = [(500.0, 500.0), (600.0, 500.0), (550.0, 600.0)]
            p.save_parcel_polygon(pa, a, closed=True)
            p.save_parcel_polygon(pb, b, closed=False)
            # Re-trace A entirely; B must be untouched.
            p.save_parcel_polygon(pa, [(0.0, 300.0), (100.0, 300.0)], closed=False)
            self.assertEqual(p.get_parcel_polygon(pb), b)
            self.assertFalse(p.get_parcel_closed(pb))

    def test_delete_parcel_leaves_others_intact(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            pa = p.create_parcel(sid)
            pb = p.create_parcel(sid)
            p.save_parcel_polygon(pa, [(0.0, 0.0), (100.0, 0.0), (0.0, 100.0)])
            p.save_parcel_polygon(pb, [(500.0, 500.0), (600.0, 500.0), (600.0, 600.0)])
            p.delete_parcel(pa)
            self.assertIsNone(p.get_parcel(pa))
            self.assertEqual(p.get_parcel_points(pa), [])  # references cascaded away
            self.assertEqual(len(p.list_parcels(sid)), 1)
            self.assertEqual(p.get_parcel_polygon(pb), [(500.0, 500.0), (600.0, 500.0), (600.0, 600.0)])

    def test_owner_round_trips(self):
        root = self.tmp / "proj"
        with ProjectDB.create(root) as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid, owner="Ramesh Kumar")
            self.assertEqual(p.get_parcel(pid)["owner"], "Ramesh Kumar")
            p.update_parcel(pid, owner="Ramesh K.")
            self.assertEqual(p.get_parcel(pid)["owner"], "Ramesh K.")
            p.update_parcel(pid, owner=None)  # cleared
            self.assertIsNone(p.get_parcel(pid)["owner"])
            p.update_parcel(pid, owner="Final")
        with ProjectDB.open(root) as p2:
            self.assertEqual(p2.get_parcel(pid)["owner"], "Final")

    def test_list_parcels_empty_when_none(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            self.assertEqual(p.list_parcels(sid), [])

    # -- shared vertices / topology (Milestone 6) ---------------------------

    def _two_adjacent_parcels(self, p):
        """Parcels A and B sharing the vertical edge x=100 (two shared corners)."""
        sid = self._source_id(p)
        pa = p.create_parcel(sid, owner="A")
        pb = p.create_parcel(sid, owner="B")
        A = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
        B = [(100.0, 0.0), (200.0, 0.0), (200.0, 100.0), (100.0, 100.0)]
        p.save_parcel_polygon(pa, A, closed=True, metres_per_pixel=0.5)
        p.save_parcel_polygon(pb, B, closed=True, metres_per_pixel=0.5)
        return sid, pa, pb

    def test_adjacent_parcels_share_edge_vertices(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid, pa, pb = self._two_adjacent_parcels(p)
            shared = set(p.get_parcel_vertex_ids(pa)) & set(p.get_parcel_vertex_ids(pb))
            self.assertEqual(len(shared), 2)                 # the common edge's two corners
            self.assertEqual(len(p.list_vertices(sid)), 6)   # 4 + 4 - 2 shared

    def test_moving_shared_vertex_updates_both_parcels_and_measures(self):
        from src.core.geometry import measure_polygon
        with ProjectDB.create(self.tmp / "proj") as p:
            sid, pa, pb = self._two_adjacent_parcels(p)
            shared = set(p.get_parcel_vertex_ids(pa)) & set(p.get_parcel_vertex_ids(pb))
            target = next(v["id"] for v in p.list_vertices(sid)
                          if v["id"] in shared and v["pixel_x"] == 100.0 and v["pixel_y"] == 0.0)
            self.assertEqual(set(p.parcels_referencing_vertex(target)), {pa, pb})
            area_a0 = measure_polygon(p.get_parcel_polygon(pa), 0.5, closed=True).area_sq_m
            area_b0 = measure_polygon(p.get_parcel_polygon(pb), 0.5, closed=True).area_sq_m
            p.move_vertex(target, 100.0, -40.0, metres_per_pixel=0.5)   # move the shared corner
            poly_a, poly_b = p.get_parcel_polygon(pa), p.get_parcel_polygon(pb)
            self.assertIn((100.0, -40.0), poly_a)            # moved for A
            self.assertIn((100.0, -40.0), poly_b)            # and for B
            self.assertNotAlmostEqual(area_a0, measure_polygon(poly_a, 0.5, closed=True).area_sq_m)
            self.assertNotAlmostEqual(area_b0, measure_polygon(poly_b, 0.5, closed=True).area_sq_m)

    def test_same_parcel_near_corners_not_merged(self):
        """Two of a parcel's OWN corners within SNAP_TOLERANCE_PX must stay
        distinct — never welded into one."""
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
            pts = [(0.0, 0.0), (3.0, 0.0), (1.0, 50.0)]      # (0,0)-(3,0) is 3px < 8px
            p.save_parcel_polygon(pid, pts)
            self.assertEqual(p.get_parcel_polygon(pid), pts)  # both corners preserved
            self.assertEqual(len(set(p.get_parcel_vertex_ids(pid))), 3)  # 3 distinct vertices

    def test_cross_parcel_near_point_merges(self):
        """A *different* parcel's point within tolerance snaps onto the vertex."""
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            pa = p.create_parcel(sid)
            pb = p.create_parcel(sid)
            p.save_parcel_polygon(pa, [(0.0, 0.0), (100.0, 0.0), (50.0, 100.0)])
            p.save_parcel_polygon(pb, [(103.0, 0.0), (300.0, 0.0), (200.0, 100.0)])  # 103 ~ 100
            self.assertEqual(p.get_parcel_polygon(pb)[0], (100.0, 0.0))  # adopted A's vertex
            shared = set(p.get_parcel_vertex_ids(pa)) & set(p.get_parcel_vertex_ids(pb))
            self.assertEqual(len(shared), 1)

    def test_parcel_with_no_shared_vertices_independent(self):
        """A parcel that shares nothing behaves exactly as before."""
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            pa = p.create_parcel(sid)
            pb = p.create_parcel(sid)
            B = [(500.0, 500.0), (600.0, 500.0), (550.0, 600.0)]
            p.save_parcel_polygon(pa, [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)], closed=True)
            p.save_parcel_polygon(pb, B, closed=True)
            self.assertEqual(set(p.get_parcel_vertex_ids(pa)) & set(p.get_parcel_vertex_ids(pb)), set())
            p.move_vertex(p.get_parcel_vertex_ids(pa)[0], 5.0, 5.0)   # move one of A's vertices
            self.assertEqual(p.get_parcel_polygon(pb), B)             # B untouched
            p.delete_parcel(pa)
            self.assertEqual(p.get_parcel_polygon(pb), B)             # still untouched

    def test_orphan_vertices_pruned_on_retrace(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
            p.save_parcel_polygon(pid, [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)])
            self.assertEqual(len(p.list_vertices(sid)), 4)
            p.save_parcel_polygon(pid, [(0.0, 0.0), (200.0, 0.0)])  # drops 3 far corners
            self.assertEqual(len(p.list_vertices(sid)), 2)          # orphans pruned

    def test_v4_points_migrate_to_shared_vertices(self):
        """A v4 file (per-parcel `points`) rebuilds into shared vertices on open,
        deduplicating the shared edge across two adjacent parcels."""
        root = self.tmp / "legacy4"
        with ProjectDB.create(root) as p:
            sid = self._source_id(p)
            pa = p.create_parcel(sid, owner="A")
            pb = p.create_parcel(sid, owner="B")
        conn = sqlite3.connect(str(root / "project.db"))
        conn.executescript(
            "DROP TABLE parcel_vertices;"
            "DROP TABLE vertices;"
            "CREATE TABLE points (id INTEGER PRIMARY KEY AUTOINCREMENT, parcel_id INTEGER,"
            " seq INTEGER, label TEXT, pixel_x REAL, pixel_y REAL, local_x REAL, local_y REAL,"
            " lat REAL, lon REAL);"
        )
        A = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
        B = [(100.0, 0.0), (200.0, 0.0), (200.0, 100.0), (100.0, 100.0)]  # shares A's right edge
        for seq, (x, y) in enumerate(A):
            conn.execute("INSERT INTO points (parcel_id, seq, pixel_x, pixel_y) VALUES (?, ?, ?, ?)",
                         (pa, seq, x, y))
        for seq, (x, y) in enumerate(B):
            conn.execute("INSERT INTO points (parcel_id, seq, pixel_x, pixel_y) VALUES (?, ?, ?, ?)",
                         (pb, seq, x, y))
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        conn.close()
        with ProjectDB.open(root) as p2:
            self.assertEqual(p2.schema_version, SCHEMA_VERSION)
            self.assertNotIn("points", p2.table_names())        # old table dropped
            self.assertEqual(p2.get_parcel_polygon(pa), A)      # geometry preserved
            self.assertEqual(p2.get_parcel_polygon(pb), B)
            shared = set(p2.get_parcel_vertex_ids(pa)) & set(p2.get_parcel_vertex_ids(pb))
            self.assertEqual(len(shared), 2)                    # shared edge deduped by migration
            self.assertEqual(len(p2.list_vertices(sid)), 6)

    # -- closed/open state persistence (v3) ---------------------------------

    def test_closed_state_defaults_open(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
            p.save_parcel_polygon(pid, [(0.0, 0.0), (10.0, 0.0), (5.0, 8.0)])  # closed not passed
            self.assertFalse(p.get_parcel_closed(pid))

    def test_open_boundary_with_three_points_stays_open(self):
        root = self.tmp / "proj"
        pts = [(0.0, 0.0), (10.0, 0.0), (5.0, 8.0)]
        with ProjectDB.create(root) as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
            p.save_parcel_polygon(pid, pts, closed=False)   # explicitly open, 3 points
        with ProjectDB.open(root) as p2:
            self.assertEqual(p2.get_parcel_polygon(pid), pts)
            self.assertFalse(p2.get_parcel_closed(pid))     # NOT auto-closed on reload

    def test_closed_boundary_round_trips_closed(self):
        root = self.tmp / "proj"
        pts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        with ProjectDB.create(root) as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
            p.save_parcel_polygon(pid, pts, closed=True)
        with ProjectDB.open(root) as p2:
            self.assertTrue(p2.get_parcel_closed(pid))

    def test_resaving_toggles_closed_state(self):
        with ProjectDB.create(self.tmp / "proj") as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
            pts = [(0.0, 0.0), (10.0, 0.0), (5.0, 8.0)]
            p.save_parcel_polygon(pid, pts, closed=True)
            self.assertTrue(p.get_parcel_closed(pid))
            p.save_parcel_polygon(pid, pts, closed=False)  # re-open
            self.assertFalse(p.get_parcel_closed(pid))

    # -- additive column migrations -----------------------------------------

    def test_v2_project_gains_closed_column_on_open(self):
        """A v2 project (parcels without the `closed` column) upgrades
        additively on open and can then store the closed state."""
        root = self.tmp / "legacy2"
        with ProjectDB.create(root) as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
            p.save_parcel_polygon(pid, [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)], closed=True)
        conn = sqlite3.connect(str(root / "project.db"))
        conn.execute("ALTER TABLE parcels DROP COLUMN closed")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()
        with ProjectDB.open(root) as p2:
            self.assertEqual(p2.schema_version, SCHEMA_VERSION)
            self.assertEqual(len(p2.get_parcel_polygon(pid)), 3)  # points preserved
            self.assertFalse(p2.get_parcel_closed(pid))           # re-added column defaults open
            p2.save_parcel_polygon(pid, p2.get_parcel_polygon(pid), closed=True)
            self.assertTrue(p2.get_parcel_closed(pid))

    def test_v3_project_gains_owner_column_on_open(self):
        """A v3 project (parcels without the `owner` column) upgrades
        additively on open and can then store the owner."""
        root = self.tmp / "legacy3"
        with ProjectDB.create(root) as p:
            sid = self._source_id(p)
            pid = p.create_parcel(sid)
        conn = sqlite3.connect(str(root / "project.db"))
        conn.execute("ALTER TABLE parcels DROP COLUMN owner")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()
        with ProjectDB.open(root) as p2:
            self.assertEqual(p2.schema_version, SCHEMA_VERSION)
            self.assertIsNone(p2.get_parcel(pid)["owner"])  # re-added column, null default
            p2.update_parcel(pid, owner="Ramesh")
            self.assertEqual(p2.get_parcel(pid)["owner"], "Ramesh")


if __name__ == "__main__":
    unittest.main()
