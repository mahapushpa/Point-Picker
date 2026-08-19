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
    nearest_edge_index, contiguous_edge_toggle,
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


class NearestEdgeTests(unittest.TestCase):
    # Square edges: 0 top, 1 right, 2 bottom, 3 left (closing).
    EDGES = [((0, 0), (100, 0)), ((100, 0), (100, 100)),
             ((100, 100), (0, 100)), ((0, 100), (0, 0))]

    def test_picks_nearest_edge_within_tolerance(self):
        self.assertEqual(nearest_edge_index((50, 1), self.EDGES, 8), 0)   # near top
        self.assertEqual(nearest_edge_index((99, 50), self.EDGES, 8), 1)  # near right

    def test_none_when_out_of_tolerance(self):
        self.assertIsNone(nearest_edge_index((50, 50), self.EDGES, 8))


class ContiguousEdgeToggleTests(unittest.TestCase):
    N = 4   # a closed square: edges 0,1,2,3 forming a cycle

    def test_first_click_starts_selection(self):
        self.assertEqual(contiguous_edge_toggle([], 2, self.N, closed=True), [2])

    def test_extend_at_either_end(self):
        self.assertEqual(contiguous_edge_toggle([1], 2, self.N, closed=True), [1, 2])
        self.assertEqual(contiguous_edge_toggle([1], 0, self.N, closed=True), [0, 1])

    def test_wrap_adjacency_when_closed(self):
        # Front is 0, so its previous (wrapping) neighbour is edge 3.
        self.assertEqual(contiguous_edge_toggle([0, 1], 3, self.N, closed=True), [3, 0, 1])

    def test_no_wrap_when_open(self):
        # Open path: edge 0 has no wrapping predecessor, so clicking 3 is a no-op.
        self.assertEqual(contiguous_edge_toggle([0, 1], 3, self.N, closed=False), [0, 1])

    def test_non_adjacent_click_is_ignored(self):
        self.assertEqual(contiguous_edge_toggle([0], 2, self.N, closed=True), [0])

    def test_remove_from_an_end(self):
        self.assertEqual(contiguous_edge_toggle([0, 1, 2], 0, self.N, closed=True), [1, 2])
        self.assertEqual(contiguous_edge_toggle([0, 1, 2], 2, self.N, closed=True), [0, 1])

    def test_interior_removal_would_split_so_ignored(self):
        self.assertEqual(contiguous_edge_toggle([0, 1, 2], 1, self.N, closed=True), [0, 1, 2])

    def test_removing_the_only_edge_clears(self):
        self.assertEqual(contiguous_edge_toggle([2], 2, self.N, closed=True), [])

    def test_full_loop_removal_leaves_contiguous_arc(self):
        # Whole closed loop selected; removing edge 1 leaves the arc 2,3,0.
        self.assertEqual(contiguous_edge_toggle([0, 1, 2, 3], 1, self.N, closed=True),
                         [2, 3, 0])

    def test_selection_never_has_a_gap(self):
        # Drive a sequence of clicks and assert contiguity throughout.
        sel = []
        for click in (1, 2, 0, 2):   # build 1,2 -> 1,2 (0 not adjacent to ends? front1/back2)
            sel = contiguous_edge_toggle(sel, click, self.N, closed=True)
        # After 1 ->[1]; 2 ->[1,2]; 0 -> prev of front(1) is 0 -> [0,1,2]; 2 -> remove end ->[0,1]
        self.assertEqual(sel, [0, 1])


if __name__ == "__main__":
    unittest.main()
