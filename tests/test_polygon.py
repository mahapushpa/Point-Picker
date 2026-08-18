"""Tests for src.core.polygon — the snap rule shared by canvas + project_db.

Pure Python; no Qt. The key rule under test: a snap candidate that is already
used by the same parcel is excluded, so a parcel never welds two of its own
corners together.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.polygon import nearest_vertex_index, SNAP_TOLERANCE_PX  # noqa: E402


class SnapTests(unittest.TestCase):
    def test_default_tolerance(self):
        self.assertEqual(SNAP_TOLERANCE_PX, 8.0)

    def test_snaps_to_vertex_within_tolerance(self):
        verts = [(10, 0.0, 0.0), (20, 100.0, 0.0)]
        self.assertEqual(nearest_vertex_index((3.0, 0.0), verts), 0)   # 3 px from vertex 10

    def test_none_when_outside_tolerance(self):
        verts = [(10, 0.0, 0.0)]
        self.assertIsNone(nearest_vertex_index((0.0, 20.0), verts))    # 20 px away

    def test_picks_nearest_among_several(self):
        verts = [(10, 0.0, 0.0), (20, 5.0, 0.0), (30, 6.0, 0.0)]
        # point at (5.5,0): nearest is vertex 20 (0.5) vs 30 (0.5) -> first-seen 20
        self.assertEqual(nearest_vertex_index((5.4, 0.0), verts), 1)

    def test_excluded_vertex_is_not_a_candidate(self):
        verts = [(10, 0.0, 0.0), (20, 100.0, 0.0)]
        # (2,0) is within tolerance of vertex 10, but 10 is excluded -> no match
        self.assertIsNone(nearest_vertex_index((2.0, 0.0), verts, exclude_ids={10}))

    def test_exclusion_falls_through_to_next_candidate(self):
        # An excluded near vertex must not block snapping to a different, allowed
        # vertex that is also within tolerance.
        verts = [(10, 0.0, 0.0), (20, 4.0, 0.0)]
        # point (3,0): vertex 10 at 3px (excluded), vertex 20 at 1px (allowed)
        self.assertEqual(nearest_vertex_index((3.0, 0.0), verts, exclude_ids={10}), 1)

    def test_same_parcel_two_corners_scenario(self):
        # Simulate placing a parcel's second corner 3px from its first: the first
        # is already used (excluded), so no snap -> caller creates a new vertex.
        existing = [(10, 0.0, 0.0)]                 # this parcel's first corner
        self.assertIsNone(nearest_vertex_index((3.0, 0.0), existing, exclude_ids={10}))

    def test_empty_candidates(self):
        self.assertIsNone(nearest_vertex_index((0.0, 0.0), []))

    def test_custom_tolerance(self):
        verts = [(10, 0.0, 0.0)]
        self.assertIsNone(nearest_vertex_index((5.0, 0.0), verts, tol=4.0))
        self.assertEqual(nearest_vertex_index((5.0, 0.0), verts, tol=6.0), 0)


if __name__ == "__main__":
    unittest.main()
