"""Tests for Milestone 16 location-fixing core (distance/trigonometry mode) and
its per-parcel persistence. All pure / stdlib-DB, so these always run.
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.location import (
    observation_from_points, target_from_field, format_description,
    cross_validate_positions, SOURCE_FIELD, SOURCE_SHEET,
)
from src.core.project_db import ProjectDB, ProjectError, SCHEMA_VERSION


class ObservationMathTests(unittest.TestCase):
    def test_distance_and_bearing_between_points(self):
        # Screen-up = North (M12): target directly above the reference reads North.
        dist, brg = observation_from_points((0, 0), (0, -10), 2.0)
        self.assertAlmostEqual(dist, 20.0)      # 10 px * 2 m/px
        self.assertAlmostEqual(brg, 0.0)        # North
        # Target to the right reads East.
        dist, brg = observation_from_points((0, 0), (10, 0), 2.0)
        self.assertAlmostEqual(dist, 20.0)
        self.assertAlmostEqual(brg, 90.0)

    def test_target_from_field_is_inverse(self):
        ref = (100.0, 100.0)
        t = target_from_field(ref, 20.0, 90.0, 2.0)   # 20 m East at 2 m/px -> +10 px x
        self.assertAlmostEqual(t[0], 110.0)
        self.assertAlmostEqual(t[1], 100.0)
        # Round-trip: computing distance/bearing back recovers the inputs.
        dist, brg = observation_from_points(ref, t, 2.0)
        self.assertAlmostEqual(dist, 20.0)
        self.assertAlmostEqual(brg, 90.0)

    def test_target_from_field_north(self):
        t = target_from_field((0, 0), 10.0, 0.0, 1.0)   # 10 m North -> -10 px y
        self.assertAlmostEqual(t[0], 0.0)
        self.assertAlmostEqual(t[1], -10.0)

    def test_scale_must_be_positive(self):
        with self.assertRaises(ValueError):
            observation_from_points((0, 0), (1, 1), 0.0)
        with self.assertRaises(ValueError):
            target_from_field((0, 0), 5.0, 10.0, -1.0)

    def test_description_is_ascii_with_deg_word(self):
        s = format_description("tubewell", 38.0, 42.0)
        self.assertEqual(s, s.encode("ascii", "ignore").decode())   # pure ASCII
        self.assertIn(" deg", s)
        self.assertNotIn("°", s)                                # no degree sign
        self.assertIn("38.0 m from tubewell", s)
        # Bearing unknown is stated, not faked.
        self.assertIn("bearing unknown", format_description("well", 12.0, None))


class CrossValidationTests(unittest.TestCase):
    def test_agreeing_references(self):
        cc = cross_validate_positions([(0, 0), (1, 0), (0, 1)], 1.0, tolerance_m=3.0)
        self.assertEqual(cc.n, 3)
        self.assertLess(cc.spread_m, 3.0)
        self.assertTrue(cc.agree)
        self.assertIn("agree", cc.describe())

    def test_one_outlier_flags_disagreement(self):
        cc = cross_validate_positions([(0, 0), (1, 0), (0, 1), (100, 0)], 1.0)
        self.assertFalse(cc.agree)
        self.assertGreater(cc.spread_m, 90.0)
        self.assertIn("DISAGREE", cc.describe())
        # The outlier is the farthest from the centroid.
        self.assertEqual(cc.deviations_m.index(max(cc.deviations_m)), 3)

    def test_single_reference_is_unverified(self):
        cc = cross_validate_positions([(5, 5)], 1.0)
        self.assertEqual(cc.n, 1)
        self.assertIn("unverified", cc.describe())

    def test_scale_applies_to_spread(self):
        far = cross_validate_positions([(0, 0), (10, 0)], 0.5)   # 10 px * 0.5 = 5 m
        self.assertAlmostEqual(far.spread_m, 5.0)
        self.assertFalse(far.agree)   # 5 m > default 3 m tolerance


class LocationFixDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = ProjectDB.create(str(Path(self._tmp.name) / "proj"))
        self.sid = self.proj.register_source("sources/x.png", "image")
        self.pid = self.proj.create_parcel(self.sid, owner="Ramesh")

    def tearDown(self):
        self.proj.close()
        self._tmp.cleanup()

    def test_schema_is_v8(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 8)
        self.assertEqual(self.proj.schema_version, SCHEMA_VERSION)

    def test_add_list_delete_roundtrip(self):
        fid = self.proj.add_location_fix(
            self.pid, "tubewell", (10.0, 20.0), 38.0,
            bearing_deg=42.0, target=(30.0, 5.0), source=SOURCE_FIELD)
        rows = self.proj.list_location_fixes(self.pid)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["label"], "tubewell")
        self.assertAlmostEqual(r["distance_m"], 38.0)
        self.assertAlmostEqual(r["bearing_deg"], 42.0)
        self.assertEqual(r["source"], "field")
        self.assertAlmostEqual(r["target_x"], 30.0)

        self.proj.delete_location_fix(fid)
        self.assertEqual(self.proj.list_location_fixes(self.pid), [])

    def test_distance_only_field_has_null_bearing_and_target(self):
        self.proj.add_location_fix(self.pid, "well", (0.0, 0.0), 12.0, source=SOURCE_FIELD)
        r = self.proj.list_location_fixes(self.pid)[0]
        self.assertIsNone(r["bearing_deg"])
        self.assertIsNone(r["target_x"])

    def test_clear_and_cascade_on_parcel_delete(self):
        self.proj.add_location_fix(self.pid, "a", (0, 0), 1.0, source=SOURCE_SHEET)
        self.proj.add_location_fix(self.pid, "b", (1, 1), 2.0, source=SOURCE_SHEET)
        self.assertEqual(len(self.proj.list_location_fixes(self.pid)), 2)
        self.proj.clear_location_fixes(self.pid)
        self.assertEqual(self.proj.list_location_fixes(self.pid), [])
        # Deleting the parcel cascades (FK ON DELETE CASCADE).
        self.proj.add_location_fix(self.pid, "c", (2, 2), 3.0, source=SOURCE_SHEET)
        self.proj.delete_parcel(self.pid)
        self.assertEqual(self.proj.list_location_fixes(self.pid), [])

    def test_bad_parcel_and_bad_source_rejected(self):
        with self.assertRaises(ProjectError):
            self.proj.add_location_fix(99999, "x", (0, 0), 1.0, source=SOURCE_FIELD)
        with self.assertRaises(ProjectError):
            self.proj.add_location_fix(self.pid, "x", (0, 0), 1.0, source="bogus")


if __name__ == "__main__":
    unittest.main()
