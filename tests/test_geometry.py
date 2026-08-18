"""Tests for src.core.geometry — pure math, no Qt.

Known polygons with hand-checkable perimeter/area.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.geometry import (  # noqa: E402
    segment_length, segment_lengths, polyline_length, polygon_perimeter,
    polygon_area, measure_polygon,
)

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]        # 10x10 px
TRIANGLE = [(0, 0), (3, 0), (0, 4)]                   # 3-4-5 right triangle


class GeometryTests(unittest.TestCase):
    def test_segment_length(self):
        self.assertAlmostEqual(segment_length((0, 0), (3, 4)), 5.0)

    def test_segment_lengths_list(self):
        self.assertEqual(segment_lengths(SQUARE), [10.0, 10.0, 10.0])  # 3 open segments

    def test_polyline_length_open(self):
        self.assertAlmostEqual(polyline_length(SQUARE), 30.0)  # no closing edge

    def test_polygon_perimeter_closed(self):
        self.assertAlmostEqual(polygon_perimeter(SQUARE), 40.0)  # includes closing edge

    def test_square_area(self):
        self.assertAlmostEqual(polygon_area(SQUARE), 100.0)

    def test_triangle_area_and_perimeter(self):
        self.assertAlmostEqual(polygon_area(TRIANGLE), 6.0)         # 0.5*3*4
        self.assertAlmostEqual(polygon_perimeter(TRIANGLE), 12.0)  # 3+4+5

    def test_area_orientation_independent(self):
        # Clockwise vs counter-clockwise give the same unsigned area.
        cw = list(reversed(SQUARE))
        self.assertAlmostEqual(polygon_area(cw), polygon_area(SQUARE))

    def test_area_zero_for_fewer_than_three(self):
        self.assertEqual(polygon_area([(0, 0)]), 0.0)
        self.assertEqual(polygon_area([(0, 0), (10, 0)]), 0.0)

    def test_measure_no_scale(self):
        m = measure_polygon(SQUARE, None, closed=True)
        self.assertFalse(m.has_scale)
        self.assertEqual(m.point_count, 4)
        self.assertAlmostEqual(m.perimeter_px, 40.0)
        self.assertAlmostEqual(m.area_px, 100.0)
        self.assertIsNone(m.perimeter_m)
        self.assertIsNone(m.area_sq_m)
        self.assertAlmostEqual(m.last_segment_px, 10.0)

    def test_measure_with_scale(self):
        # 0.5 m/px: perimeter 40 px -> 20 m; area 100 px^2 -> 25 m^2
        m = measure_polygon(SQUARE, 0.5, closed=True)
        self.assertTrue(m.has_scale)
        self.assertAlmostEqual(m.perimeter_m, 20.0)
        self.assertAlmostEqual(m.area_sq_m, 25.0)
        self.assertAlmostEqual(m.last_segment_m, 5.0)

    def test_measure_open_vs_closed_perimeter(self):
        open_m = measure_polygon(SQUARE, None, closed=False)
        closed_m = measure_polygon(SQUARE, None, closed=True)
        self.assertAlmostEqual(open_m.perimeter_px, 30.0)   # running polyline
        self.assertAlmostEqual(closed_m.perimeter_px, 40.0)  # + closing edge
        # Area is the closed-ring shoelace either way.
        self.assertAlmostEqual(open_m.area_px, 100.0)

    def test_measure_rejects_bad_scale(self):
        with self.assertRaises(ValueError):
            measure_polygon(SQUARE, 0.0)

    def test_real_survey_scale_area(self):
        # A 100m x 50m field traced at 0.05 m/px would be 2000x1000 px.
        rect = [(0, 0), (2000, 0), (2000, 1000), (0, 1000)]
        m = measure_polygon(rect, 0.05, closed=True)
        self.assertAlmostEqual(m.area_sq_m, 5000.0)      # 100*50
        self.assertAlmostEqual(m.perimeter_m, 300.0)     # 2*(100+50)


if __name__ == "__main__":
    unittest.main()
