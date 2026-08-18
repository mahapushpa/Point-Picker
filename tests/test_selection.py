"""Tests for src.core.selection — the pure-Python parcel hit-testing that
backs Milestone 7 multi-selection (click toggle + marquee). No Qt involved."""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.selection import (
    point_in_polygon, point_hits_parcel, polygon_intersects_rect,
    parcel_at_point, parcels_in_rect, dist_point_to_polygon, CLICK_TOLERANCE_PX,
)

# A 100x100 square parcel with corner at the origin.
SQUARE = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]


class PointInPolygonTests(unittest.TestCase):
    def test_inside(self):
        self.assertTrue(point_in_polygon((50, 50), SQUARE))

    def test_outside(self):
        self.assertFalse(point_in_polygon((150, 50), SQUARE))
        self.assertFalse(point_in_polygon((-5, 50), SQUARE))

    def test_degenerate_polygon_is_never_inside(self):
        self.assertFalse(point_in_polygon((0, 0), [(0, 0), (10, 0)]))
        self.assertFalse(point_in_polygon((0, 0), []))


class PointHitsParcelTests(unittest.TestCase):
    def test_click_inside_fill_hits(self):
        self.assertTrue(point_hits_parcel((50, 50), SQUARE))

    def test_click_on_boundary_hits(self):
        # Right on the top edge.
        self.assertTrue(point_hits_parcel((50, 0), SQUARE))

    def test_click_just_outside_boundary_within_tolerance_hits(self):
        self.assertTrue(point_hits_parcel((50, -CLICK_TOLERANCE_PX + 1), SQUARE))

    def test_click_far_outside_misses(self):
        self.assertFalse(point_hits_parcel((50, -40), SQUARE))

    def test_open_trace_selectable_by_proximity_to_an_edge(self):
        # A 2-point open trace has no fill, but a click near its single edge hits.
        trace = [(0.0, 0.0), (100.0, 0.0)]
        self.assertTrue(point_hits_parcel((50, 2), trace))
        self.assertFalse(point_hits_parcel((50, 40), trace))

    def test_dist_to_polygon_uses_closing_edge(self):
        # A point just outside the left (closing) edge is close to the boundary.
        self.assertAlmostEqual(dist_point_to_polygon((-3, 50), SQUARE), 3.0, places=6)


class MarqueeTests(unittest.TestCase):
    def test_marquee_containing_vertices_selects(self):
        self.assertTrue(polygon_intersects_rect(SQUARE, (-10, -10, 200, 200)))

    def test_marquee_fully_outside_does_not_select(self):
        self.assertFalse(polygon_intersects_rect(SQUARE, (200, 200, 300, 300)))

    def test_marquee_merely_overlapping_edge_selects(self):
        # Rect straddles the right edge — no parcel vertex inside it, but edges
        # cross, so a touching parcel is caught.
        self.assertTrue(polygon_intersects_rect(SQUARE, (90, 40, 130, 60)))

    def test_marquee_inside_parcel_selects(self):
        # A small rect wholly within a large closed parcel still counts (touching
        # or within).
        self.assertTrue(polygon_intersects_rect(SQUARE, (40, 40, 60, 60)))

    def test_marquee_unnormalized_rect_ok(self):
        # Dragged bottom-right -> top-left: coordinates arrive reversed.
        self.assertTrue(polygon_intersects_rect(SQUARE, (200, 200, -10, -10)))


class ParcelLookupTests(unittest.TestCase):
    def _parcels(self):
        far = [(500.0, 500.0), (600.0, 500.0), (600.0, 600.0), (500.0, 600.0)]
        return [(1, SQUARE), (2, far)]

    def test_parcel_at_point_finds_the_right_one(self):
        self.assertEqual(parcel_at_point(self._parcels(), (50, 50)), 1)
        self.assertEqual(parcel_at_point(self._parcels(), (550, 550)), 2)

    def test_parcel_at_point_none_on_empty_space(self):
        self.assertIsNone(parcel_at_point(self._parcels(), (300, 300)))

    def test_parcel_at_point_topmost_wins_on_overlap(self):
        # Two parcels covering the same spot: the later (topmost) one wins.
        overlap = [(1, SQUARE), (2, SQUARE)]
        self.assertEqual(parcel_at_point(overlap, (50, 50)), 2)

    def test_parcels_in_rect_returns_all_touched_in_order(self):
        parcels = self._parcels()
        self.assertEqual(parcels_in_rect(parcels, (-10, -10, 700, 700)), [1, 2])
        self.assertEqual(parcels_in_rect(parcels, (-10, -10, 120, 120)), [1])


if __name__ == "__main__":
    unittest.main()
