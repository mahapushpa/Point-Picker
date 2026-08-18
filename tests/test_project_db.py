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
                        "parcel_fields", "points"}
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


if __name__ == "__main__":
    unittest.main()
