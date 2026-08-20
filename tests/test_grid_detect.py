"""Tests for Milestone 19: ruled-grid detection (io) and the grid scale + N-way
cross-check (core). All pure; numpy is a project dependency, so these always run.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from src.io.raster import RasterImage
from src.io.grid_detect import detect_grid_spacing
from src.core.scale import grid_scale, cross_check_scales, METHOD_GRID


def _raster(gray: np.ndarray) -> RasterImage:
    h, w = gray.shape
    a = np.empty((h, w, 4), dtype=np.uint8)
    a[:, :, 0] = a[:, :, 1] = a[:, :, 2] = gray
    a[:, :, 3] = 255
    return RasterImage(w, h, a.tobytes(), "RGBA")


def _grid(spacing_x=20, spacing_y=20, size=200):
    g = np.full((size, size), 255, dtype=np.uint8)
    for x in range(spacing_x, size - 1, spacing_x):
        g[:, x] = 0
    for y in range(spacing_y, size - 1, spacing_y):
        g[y, :] = 0
    return _raster(g)


class DetectGridTests(unittest.TestCase):
    def test_clear_grid_detected_with_correct_spacing(self):
        d = detect_grid_spacing(_grid(20, 20))
        self.assertTrue(d.found)
        self.assertAlmostEqual(d.spacing_px, 20.0, delta=0.5)
        self.assertGreaterEqual(d.row_peaks, 5)
        self.assertGreaterEqual(d.col_peaks, 5)

    def test_grid_free_image_no_false_positive(self):
        d = detect_grid_spacing(_raster(np.full((200, 200), 255, dtype=np.uint8)))
        self.assertFalse(d.found)
        self.assertIsNone(d.spacing_px)

    def test_single_axis_ruling_is_not_a_grid(self):
        # Horizontal lines only: columns are uniform, so only one axis rules -> the
        # strict both-axes rule rejects it (a border/table could look like this).
        g = np.full((200, 200), 255, dtype=np.uint8)
        for y in range(20, 199, 20):
            g[y, :] = 0
        d = detect_grid_spacing(_raster(g))
        self.assertFalse(d.found)
        self.assertIn("only rows", d.reason)

    def test_irregular_lines_are_not_a_grid(self):
        # Irregularly-spaced vertical lines (not a ruled grid) -> fails the CV gate.
        g = np.full((200, 200), 255, dtype=np.uint8)
        for x in (10, 55, 90, 140, 175):
            g[:, x] = 0
        d = detect_grid_spacing(_raster(g))
        self.assertFalse(d.found)

    def test_axes_disagree_is_not_a_grid(self):
        # Both axes regular but different spacings -> not a consistent grid.
        d = detect_grid_spacing(_grid(spacing_x=30, spacing_y=20))
        self.assertFalse(d.found)
        self.assertIn("disagree", d.reason)


class GridScaleTests(unittest.TestCase):
    def test_grid_scale_math(self):
        gs = grid_scale(20.0, 10.0)          # 20 px per cell, 10 m per cell
        self.assertEqual(gs.method, METHOD_GRID)
        self.assertAlmostEqual(gs.metres_per_pixel, 0.5)
        self.assertAlmostEqual(gs.pixels_per_metre, 2.0)

    def test_grid_scale_rejects_nonpositive(self):
        for bad in [(0.0, 10.0), (20.0, 0.0), (-1.0, 10.0)]:
            with self.assertRaises(ValueError):
                grid_scale(*bad)


class CrossCheckTests(unittest.TestCase):
    def test_three_way_agreement(self):
        cc = cross_check_scales({"Manual": 0.500, "PDF metadata": 0.503, "Grid": 0.499})
        self.assertEqual(len(cc.labels), 3)
        self.assertTrue(cc.agree)
        self.assertLess(cc.max_percent_difference, 2.0)
        self.assertIn("agree", cc.describe())

    def test_three_way_disagreement_names_worst_pair(self):
        cc = cross_check_scales({"Manual": 0.50, "PDF metadata": 0.51, "Grid": 0.80})
        self.assertFalse(cc.agree)
        self.assertGreater(cc.max_percent_difference, 40.0)
        self.assertEqual(set(cc.worst_pair), {"Manual", "Grid"})   # 0.50 vs 0.80
        self.assertIn("DISAGREE", cc.describe())

    def test_two_way_still_works(self):
        cc = cross_check_scales({"Manual": 0.50, "Grid": 0.505})
        self.assertTrue(cc.agree)

    def test_single_scale_says_no_cross_check(self):
        cc = cross_check_scales({"Grid": 0.50})
        self.assertTrue(cc.agree)
        self.assertIsNone(cc.worst_pair)
        self.assertIn("no independent method", cc.describe())

    def test_custom_tolerance(self):
        self.assertFalse(cross_check_scales({"A": 0.50, "B": 0.52}).agree)          # ~3.9%
        self.assertTrue(cross_check_scales({"A": 0.50, "B": 0.52},
                                           tolerance_percent=5.0).agree)

    def test_nonpositive_rejected(self):
        with self.assertRaises(ValueError):
            cross_check_scales({"A": 0.0, "B": 0.5})


if __name__ == "__main__":
    unittest.main()
