"""Tests for Milestone 18 boundary line-following (intelligent-scissors search).

Pure logic (numpy present), so these always run. The search must produce a sane
result on a clear line, degrade without crashing on a faint one, and never mutate.
"""

import sys
import unittest
from math import hypot
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from src.io.raster import RasterImage
from src.io.tracing_assist import follow_line, AssistUnavailable, MAX_ROI_PIXELS


def _raster(gray: np.ndarray) -> RasterImage:
    h, w = gray.shape
    a = np.empty((h, w, 4), dtype=np.uint8)
    a[:, :, 0] = a[:, :, 1] = a[:, :, 2] = gray
    a[:, :, 3] = 255
    return RasterImage(w, h, a.tobytes(), "RGBA")


def _L_line():
    """A thin dark L on white: vertical x~40 (y 40..120), horizontal y~120 (x 40..160)."""
    g = np.full((200, 200), 255, dtype=np.uint8)
    g[40:121, 39:42] = 0
    g[119:122, 40:161] = 0
    return _raster(g)


class FollowLineTests(unittest.TestCase):
    def test_endpoints_preserved(self):
        r = _L_line()
        path = follow_line(r, (40, 40), (160, 120))
        self.assertEqual(path[0], (40.0, 40.0))
        self.assertEqual(path[-1], (160.0, 120.0))
        self.assertGreaterEqual(len(path), 2)

    def test_follows_the_bend_not_the_chord(self):
        r = _L_line()
        path = follow_line(r, (40, 40), (160, 120))
        chord = hypot(160 - 40, 120 - 40)
        plen = sum(hypot(path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1])
                   for i in range(len(path) - 1))
        # Following the L is meaningfully longer than cutting straight across.
        self.assertGreater(plen, chord * 1.2)
        # A vertex lands near the true corner (40, 120).
        dcorner = min(hypot(x - 40, y - 120) for x, y in path)
        self.assertLess(dcorner, 6)

    def test_path_sits_on_ink(self):
        r = _L_line()
        g = np.frombuffer(r.data, np.uint8).reshape(200, 200, 4)[:, :, 0]
        path = follow_line(r, (40, 40), (160, 120))
        for x, y in path:
            self.assertLess(g[int(round(y)), int(round(x))], 128)   # dark

    def test_straight_line_simplifies_to_two_points(self):
        g = np.full((120, 120), 255, dtype=np.uint8)
        g[59:62, 10:110] = 0                     # a straight horizontal line
        path = follow_line(_raster(g), (12, 60), (108, 60))
        self.assertEqual(len(path), 2)

    def test_faint_ambiguous_degrades_without_crashing(self):
        rng = np.random.RandomState(0)
        g = np.clip(200 + rng.randint(-3, 3, (150, 150)), 0, 255).astype(np.uint8)
        path = follow_line(_raster(g), (20, 20), (130, 130))
        self.assertGreaterEqual(len(path), 2)     # something reviewable, no crash
        self.assertEqual(path[0], (20.0, 20.0))
        self.assertEqual(path[-1], (130.0, 130.0))

    def test_coincident_points_return_single(self):
        self.assertEqual(follow_line(_L_line(), (50, 50), (50, 50)), [(50.0, 50.0)])

    def test_far_apart_points_are_refused_not_hung(self):
        # A large sheet with the two marks at opposite corners: the search ROI would
        # exceed the node budget, so it refuses (fall back to manual) rather than
        # running an unbounded pure-Python Dijkstra.
        side = int(MAX_ROI_PIXELS ** 0.5) + 400
        big = _raster(np.full((side, side), 255, dtype=np.uint8))
        with self.assertRaises(AssistUnavailable):
            follow_line(big, (5, 5), (side - 5, side - 5))

    def test_within_budget_still_runs(self):
        # A pair whose ROI is comfortably under budget still works.
        g = np.full((400, 400), 255, dtype=np.uint8)
        g[199:202, 20:380] = 0
        path = follow_line(_raster(g), (22, 200), (378, 200))
        self.assertGreaterEqual(len(path), 2)

    def test_does_not_mutate_inputs(self):
        r = _L_line()
        before = bytes(r.data)
        follow_line(r, (40, 40), (160, 120))
        self.assertEqual(bytes(r.data), before)


if __name__ == "__main__":
    unittest.main()
