"""Tests for src.core.units — built-in conversions and the UnitProfile type.

Pure Python; verifies the fixed built-in factors against known values and the
conversion helpers. Local-profile round-trips through the DB live in
test_project_db.py; measurement integration lives in test_main_units.py.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core import units


class BuiltinFactorTests(unittest.TestCase):
    def test_area_factors_are_exact_known_values(self):
        a = units.BUILTIN_AREA_UNITS
        self.assertEqual(a["square metre"], 1.0)
        self.assertAlmostEqual(a["square foot"], 0.3048 ** 2, places=12)   # 0.09290304
        self.assertEqual(a["hectare"], 10000.0)
        self.assertAlmostEqual(a["acre"], 43560 * 0.3048 ** 2, places=6)   # 4046.8564224

    def test_length_factors(self):
        self.assertEqual(units.BUILTIN_LENGTH_UNITS["metre"], 1.0)
        self.assertEqual(units.BUILTIN_LENGTH_UNITS["foot"], 0.3048)


class ConversionTests(unittest.TestCase):
    def test_area_in_unit_known_values(self):
        # 1 hectare == 10000 m²; 2 ha from 20000 m².
        self.assertAlmostEqual(units.area_in_unit(20000.0, 10000.0), 2.0)
        # 4046.8564224 m² is exactly 1 acre.
        self.assertAlmostEqual(units.area_in_unit(4046.8564224, units.BUILTIN_AREA_UNITS["acre"]), 1.0)
        # 1 m² in square feet.
        self.assertAlmostEqual(units.area_in_unit(1.0, units.BUILTIN_AREA_UNITS["square foot"]),
                               1 / 0.09290304, places=9)

    def test_length_in_unit(self):
        self.assertAlmostEqual(units.length_in_unit(3.048, 0.3048), 10.0)

    def test_area_in_profile(self):
        p = units.UnitProfile("Bigha — X", 2529.28)
        self.assertAlmostEqual(units.area_in_profile(2529.28, p), 1.0)

    def test_non_positive_factor_rejected(self):
        with self.assertRaises(ValueError):
            units.area_in_unit(10.0, 0.0)
        with self.assertRaises(ValueError):
            units.length_in_unit(10.0, -1.0)


class UnitProfileTests(unittest.TestCase):
    def test_valid_profile(self):
        p = units.UnitProfile("Bigha — Jaipur", 2529.28, is_builtin=False, id=7)
        self.assertEqual(p.name, "Bigha — Jaipur")
        self.assertEqual(p.sq_m_per_unit, 2529.28)

    def test_invalid_profiles_rejected(self):
        with self.assertRaises(ValueError):
            units.UnitProfile("", 100.0)
        with self.assertRaises(ValueError):
            units.UnitProfile("   ", 100.0)
        with self.assertRaises(ValueError):
            units.UnitProfile("bad", 0.0)
        with self.assertRaises(ValueError):
            units.UnitProfile("bad", -5.0)


if __name__ == "__main__":
    unittest.main()
