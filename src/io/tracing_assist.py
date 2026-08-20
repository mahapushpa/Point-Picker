"""tracing_assist — semi-automated boundary line-following (Milestone 18).

Pure Python, no UI-framework imports. This is the "intelligent scissors" /
"magnetic lasso" technique — a least-cost (Dijkstra) path search over an
edge/darkness cost image between two user-marked points — implemented directly
against numpy + stdlib ``heapq`` rather than pulling in a CV library, keeping the
app's dependency footprint unchanged (numpy/Pillow are already required).

The followed path is only ever a *suggestion*: the caller shows it for explicit
confirmation and never auto-accepts it (the source project's hard lesson — a
wrong auto-path on an official land record is worse than a slower manual trace).
On an ambiguous/faint line the search still returns *some* least-cost path (it
never crashes), which the user can simply reject and trace by hand instead.

Only meaningful for scanned/rendered pixel sources; a DXF's boundary is exact
vector geometry rendered to a clean raster we generate, with no ink to follow, so
callers disable the assist for DXF (same reasoning as M17's missing-corner check).

Why this lives in ``io/`` and not ``core/``: like :mod:`src.io.guardrails` and
:mod:`src.io.preprocess`, it is a heuristic that reads a source's *decoded raster
pixels* (a :class:`~src.io.raster.RasterImage`). ``io/`` owns everything tied to a
source's pixel/byte representation — the file-format loaders (``pdf_loader`` /
``dxf_loader``) *decode* files into that representation, and these modules
*analyse* it. ``core/`` is deliberately source-representation-independent domain
logic (geometry, scale, units, polygon/topology) with no pixel dependency, so a
pixel-sampling search belongs here, not there.
"""

from __future__ import annotations

import heapq
from math import hypot, sqrt

from .raster import RasterImage

Point = tuple[float, float]

# --- cost model ------------------------------------------------------------
#: Cost blends darkness (printed boundaries are dark ink -> cheap to traverse)
#: with inverted gradient magnitude (strong edges -> cheap), plus a floor so a
#: path is still penalised for cutting across blank paper. All terms are 0..1.
W_BRIGHT = 1.0
W_GRAD = 0.5
COST_FLOOR = 0.1

#: The search is restricted to the bounding box of the two marks, grown by this
#: margin (px), so a pure-Python heap search stays fast — the two marks are close
#: along a boundary. A path that would bend outside this window is (correctly) not
#: found and the user rejects/traces manually.
MARGIN_PX = 48

#: Ramer-Douglas-Peucker tolerance (px) for turning the per-pixel path into a sane
#: set of vertices — corner-preserving, so real bends survive (and remain visible
#: to M17's missing-corner check) instead of becoming thousands of points.
SIMPLIFY_PX = 2.0

#: Node budget for the Dijkstra search: the ROI (the two marks' bounding box grown
#: by MARGIN_PX) may not exceed this many pixels. A pure-Python heap search over
#: more than ~1M nodes would hang noticeably, and two marks that far apart aren't a
#: line-follow anyway. This guard matters specifically for large scanned village
#: sheets, where a stray distant pair of clicks could otherwise freeze the app for
#: seconds — instead the caller is told to trace that segment manually.
MAX_ROI_PIXELS = 1_200_000


class AssistUnavailable(Exception):
    """Raised when the two marks are too far apart to follow within the search
    budget (:data:`MAX_ROI_PIXELS`). The caller should fall back to manual tracing
    rather than hang on an unbounded search."""

_SQRT2 = sqrt(2.0)
_NEIGHBOURS = ((-1, -1, _SQRT2), (-1, 0, 1.0), (-1, 1, _SQRT2),
               (0, -1, 1.0), (0, 1, 1.0),
               (1, -1, _SQRT2), (1, 0, 1.0), (1, 1, _SQRT2))


def _grayscale(raster: RasterImage):
    import numpy as np
    if raster.mode != "RGBA":
        raise ValueError(f"tracing_assist expects RGBA rasters, got mode {raster.mode!r}")
    arr = np.frombuffer(raster.data, dtype=np.uint8).reshape(raster.height, raster.width, 4)
    return arr[:, :, :3].astype(np.float64) @ (0.299, 0.587, 0.114)


def _cost_image(gray):
    """Per-pixel traversal cost: low on dark ink / strong edges, high on paper."""
    import numpy as np
    gx, gy = np.gradient(gray)
    grad = np.hypot(gx, gy)
    gradnorm = grad / (grad.max() + 1e-9)
    bright = gray / 255.0
    return W_BRIGHT * bright + W_GRAD * (1.0 - gradnorm) + COST_FLOOR


def _dijkstra(cost, src, dst):
    """Least-cost 8-connected path from *src* to *dst* (each ``(col, row)``) over
    the *cost* array. Edge weight is the destination pixel's cost times the step
    length (diagonal = sqrt(2)), which avoids diagonal shortcutting. Returns a list
    of ``(col, row)`` from src to dst."""
    import numpy as np
    h, w = cost.shape
    n = h * w
    sc, sr = src
    dc, dr = dst
    src_i = sr * w + sc
    dst_i = dr * w + dc
    if src_i == dst_i:
        return [(sc, sr)]
    dist = np.full(n, np.inf)
    prev = np.full(n, -1, dtype=np.int64)
    dist[src_i] = 0.0
    heap = [(0.0, src_i)]
    costf = cost  # local ref
    while heap:
        d, u = heapq.heappop(heap)
        if u == dst_i:
            break
        if d > dist[u]:
            continue
        ur, uc = divmod(u, w)
        for dr_, dc_, step in _NEIGHBOURS:
            vr = ur + dr_
            vc = uc + dc_
            if 0 <= vr < h and 0 <= vc < w:
                nd = d + costf[vr, vc] * step
                vi = vr * w + vc
                if nd < dist[vi]:
                    dist[vi] = nd
                    prev[vi] = u
                    heapq.heappush(heap, (nd, vi))
    # Reconstruct (dst is always reachable in a fully-connected grid).
    path = []
    i = dst_i
    if prev[i] == -1 and i != src_i:
        return [(sc, sr), (dc, dr)]   # defensive: fall back to the straight ends
    while i != -1:
        r, c = divmod(int(i), w)
        path.append((c, r))
        if i == src_i:
            break
        i = int(prev[i])
    path.reverse()
    return path


def _rdp(points, tol):
    """Ramer-Douglas-Peucker simplification of a polyline (keeps endpoints)."""
    if len(points) < 3:
        return list(points)
    a, b = points[0], points[-1]
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg = hypot(dx, dy)
    # Perpendicular distance of each interior point from the a-b chord.
    worst_i, worst_d = 0, -1.0
    for i in range(1, len(points) - 1):
        px, py = points[i]
        if seg == 0:
            dpar = hypot(px - ax, py - ay)
        else:
            dpar = abs(dy * px - dx * py + bx * ay - by * ax) / seg
        if dpar > worst_d:
            worst_d, worst_i = dpar, i
    if worst_d > tol:
        left = _rdp(points[:worst_i + 1], tol)
        right = _rdp(points[worst_i:], tol)
        return left[:-1] + right
    return [a, b]


def follow_line(raster: RasterImage, start: Point, end: Point, *,
                margin_px: int = MARGIN_PX,
                simplify_px: float = SIMPLIFY_PX) -> list[Point]:
    """Follow a printed/drawn boundary line from *start* to *end* (image-pixel
    (x, y)); return the followed path as a simplified list of (x, y) vertices,
    endpoints included.

    Least-cost search over a darkness/gradient cost image, restricted to the two
    points' bounding box plus *margin_px*. Never raises on a faint/ambiguous line —
    it returns the least-cost path found (which the caller shows for confirm /
    reject). Raises :class:`AssistUnavailable` if the two marks are so far apart
    that the search area exceeds :data:`MAX_ROI_PIXELS` (fall back to manual
    tracing), and ``ValueError`` for a degenerate raster.
    """
    gray = _grayscale(raster)
    h, w = gray.shape
    sx = min(max(int(round(start[0])), 0), w - 1)
    sy = min(max(int(round(start[1])), 0), h - 1)
    ex = min(max(int(round(end[0])), 0), w - 1)
    ey = min(max(int(round(end[1])), 0), h - 1)
    if (sx, sy) == (ex, ey):
        return [(float(sx), float(sy))]

    x0 = max(0, min(sx, ex) - margin_px)
    x1 = min(w, max(sx, ex) + margin_px + 1)
    y0 = max(0, min(sy, ey) - margin_px)
    y1 = min(h, max(sy, ey) + margin_px + 1)
    roi_area = (x1 - x0) * (y1 - y0)
    if roi_area > MAX_ROI_PIXELS:
        raise AssistUnavailable(
            f"the two points are too far apart to follow automatically "
            f"({roi_area:,}-pixel search area exceeds the {MAX_ROI_PIXELS:,} "
            "budget); trace this segment manually instead")
    cost = _cost_image(gray[y0:y1, x0:x1])

    path_local = _dijkstra(cost, (sx - x0, sy - y0), (ex - x0, ey - y0))
    path = [(c + x0, r + y0) for c, r in path_local]
    simplified = _rdp(path, simplify_px)
    return [(float(x), float(y)) for x, y in simplified]
