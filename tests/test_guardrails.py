"""Tests for Milestone 17 point-data guard rails (warnings only).

Both checks are pure and always run (numpy is a project dependency). They must
never mutate input — these tests also assert the point list is unchanged.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from src.io.raster import RasterImage
from src.io.guardrails import (
    is_duplicate_point, find_missing_corner_edges, DUP_TOLERANCE_PX,
)


def _raster_from_gray(gray: np.ndarray) -> RasterImage:
    """Build an RGBA RasterImage from an HxW uint8 grayscale array."""
    h, w = gray.shape
    arr = np.empty((h, w, 4), dtype=np.uint8)
    arr[:, :, 0] = arr[:, :, 1] = arr[:, :, 2] = gray
    arr[:, :, 3] = 255
    return RasterImage(w, h, arr.tobytes(), "RGBA")


def _L_shape_raster():
    """A white sheet with a thick black L: a vertical arm at x~30 (y 30..150) and a
    horizontal arm at y~150 (x 30..150). Corner at (30, 150)."""
    gray = np.full((200, 200), 255, dtype=np.uint8)
    gray[30:151, 27:34] = 0      # vertical arm (x ~ 30)
    gray[147:154, 30:151] = 0    # horizontal arm (y ~ 150)
    return _raster_from_gray(gray)


class DuplicatePointTests(unittest.TestCase):
    def test_triggers_on_near_coincident(self):
        self.assertTrue(is_duplicate_point((100, 100), (103, 102)))   # ~3.6 px
        self.assertTrue(is_duplicate_point((100, 100), (100, 100)))   # exact

    def test_quiet_on_close_but_distinct(self):
        # Just beyond the snap radius: a legitimately close-but-distinct corner.
        self.assertFalse(is_duplicate_point((100, 100), (100 + DUP_TOLERANCE_PX + 1, 100)))
        self.assertFalse(is_duplicate_point((100, 100), (140, 100)))  # clearly distinct

    def test_boundary_is_inclusive(self):
        self.assertTrue(is_duplicate_point((0, 0), (DUP_TOLERANCE_PX, 0)))


class MissingCornerTests(unittest.TestCase):
    def test_clean_trace_along_the_L_is_quiet(self):
        raster = _L_shape_raster()
        # Trace that follows both arms with the corner marked — every edge tracks ink.
        pts = [(30, 30), (30, 150), (150, 150)]
        before = list(pts)
        warnings = find_missing_corner_edges(raster, pts, closed=False)
        self.assertEqual(warnings, [])
        self.assertEqual(pts, before)   # never mutated

    def test_cut_corner_edge_is_flagged(self):
        raster = _L_shape_raster()
        # A single straight edge from the top of the vertical arm to the end of the
        # horizontal arm — endpoints on ink, but the chord cuts across blank paper,
        # skipping the real corner at (30, 150).
        pts = [(30, 30), (150, 150)]
        warnings = find_missing_corner_edges(raster, pts, closed=False)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].edge_index, 0)
        self.assertLess(warnings[0].coverage, 0.6)

    def test_short_edges_never_flagged(self):
        raster = _L_shape_raster()
        # A tiny cut across the corner, below MIN_EDGE_PX — stays quiet regardless.
        pts = [(30, 145), (35, 150)]
        self.assertEqual(find_missing_corner_edges(raster, pts, closed=False), [])

    def test_edge_off_ink_stays_quiet(self):
        # Endpoints not on any ink: we can't judge, so no warning (conservative).
        raster = _raster_from_gray(np.full((200, 200), 255, dtype=np.uint8))  # blank
        pts = [(10, 10), (180, 180)]
        self.assertEqual(find_missing_corner_edges(raster, pts, closed=False), [])

    def test_closing_edge_is_considered_when_closed(self):
        raster = _L_shape_raster()
        # Both drawn edges follow the L's arms; only the closing edge (last->first)
        # cuts diagonally across the corner. So it's quiet when open, flagged when
        # closed — and the flag is the closing-edge index.
        pts = [(30, 30), (30, 150), (150, 150)]
        open_warn = find_missing_corner_edges(raster, pts, closed=False)
        closed_warn = find_missing_corner_edges(raster, pts, closed=True)
        self.assertEqual(open_warn, [])
        self.assertEqual([w.edge_index for w in closed_warn], [len(pts) - 1])


if __name__ == "__main__":
    unittest.main()
