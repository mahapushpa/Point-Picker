"""Tests for src.core.scale — pure math, no Qt.

Standard-library unittest; no third-party install needed.
"""

import math
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.scale import (  # noqa: E402
    compute_two_point_scale, pixel_distance, TwoPointScale, METHOD_TWO_POINT,
    is_coarse_scale, COARSE_SCALE_WARN_M_PER_PX,
)


class CoarseScaleTests(unittest.TestCase):
    def test_none_is_never_coarse(self):
        self.assertFalse(is_coarse_scale(None))

    def test_fine_scale_not_flagged(self):
        self.assertFalse(is_coarse_scale(0.02))          # 2 cm/px — fine
        self.assertFalse(is_coarse_scale(COARSE_SCALE_WARN_M_PER_PX))  # exactly at threshold

    def test_coarse_scale_flagged(self):
        self.assertTrue(is_coarse_scale(COARSE_SCALE_WARN_M_PER_PX + 0.001))
        self.assertTrue(is_coarse_scale(0.5))            # 50 cm/px — clearly coarse

    def test_threshold_override(self):
        self.assertTrue(is_coarse_scale(0.06, threshold=0.05))
        self.assertFalse(is_coarse_scale(0.04, threshold=0.05))


class ScaleMathTests(unittest.TestCase):
    def test_pixel_distance_horizontal(self):
        self.assertEqual(pixel_distance((0, 0), (10, 0)), 10.0)

    def test_pixel_distance_diagonal(self):
        self.assertAlmostEqual(pixel_distance((0, 0), (3, 4)), 5.0)

    def test_scale_simple(self):
        # 100 px apart = 50 m  ->  0.5 m/px
        s = compute_two_point_scale((0, 0), (100, 0), 50.0)
        self.assertIsInstance(s, TwoPointScale)
        self.assertAlmostEqual(s.metres_per_pixel, 0.5)
        self.assertAlmostEqual(s.pixel_distance, 100.0)
        self.assertEqual(s.real_distance_m, 50.0)
        self.assertEqual(s.method, METHOD_TWO_POINT)

    def test_scale_diagonal(self):
        # (0,0)-(3,4) is 5 px; 5 px == 20 m -> 4 m/px
        s = compute_two_point_scale((0, 0), (3, 4), 20.0)
        self.assertAlmostEqual(s.metres_per_pixel, 4.0)

    def test_pixels_per_metre_is_inverse(self):
        s = compute_two_point_scale((0, 0), (100, 0), 25.0)
        self.assertAlmostEqual(s.metres_per_pixel * s.pixels_per_metre, 1.0)
        self.assertAlmostEqual(s.pixels_per_metre, 4.0)  # 100px / 25m

    def test_order_of_points_does_not_matter(self):
        a = compute_two_point_scale((10, 20), (110, 20), 50.0)
        b = compute_two_point_scale((110, 20), (10, 20), 50.0)
        self.assertAlmostEqual(a.metres_per_pixel, b.metres_per_pixel)

    def test_float_pixel_coords(self):
        s = compute_two_point_scale((1234.5, 678.9), (1234.5, 778.9), 12.5)
        self.assertAlmostEqual(s.pixel_distance, 100.0)
        self.assertAlmostEqual(s.metres_per_pixel, 0.125)

    def test_zero_distance_rejected(self):
        with self.assertRaises(ValueError):
            compute_two_point_scale((0, 0), (100, 0), 0.0)

    def test_negative_distance_rejected(self):
        with self.assertRaises(ValueError):
            compute_two_point_scale((0, 0), (100, 0), -5.0)

    def test_coincident_points_rejected(self):
        with self.assertRaises(ValueError):
            compute_two_point_scale((42, 42), (42, 42), 10.0)

    def test_result_is_immutable(self):
        s = compute_two_point_scale((0, 0), (10, 0), 5.0)
        with self.assertRaises(Exception):
            s.metres_per_pixel = 0.1  # frozen dataclass


if __name__ == "__main__":
    unittest.main()
