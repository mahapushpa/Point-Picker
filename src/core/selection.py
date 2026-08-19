"""selection — pure-Python hit-testing for parcel multi-selection (Milestone 7).

No UI-framework imports (architecture rule). The canvas turns a click or a
marquee drag into scene coordinates and hands them here; these predicates decide
which parcels a click lands on or a rectangle catches, so the selection logic is
testable without Qt.

A parcel is its ordered list of boundary points ``[(x, y), ...]`` in image
pixels. "Selecting by boundary or proximity" (the brief) means a click counts if
it lands inside a closed boundary *or* close to any edge/vertex, and a marquee
catches a parcel that is touching *or* within the dragged region — neighbouring
khasras are typically adjacent, so mere overlap must count, not full containment.
"""

from __future__ import annotations

from math import hypot

Point = tuple[float, float]
Rect = tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y), unordered ok

#: A click within this many pixels of a boundary edge/vertex still hits the
#: parcel, so thin boundaries and not-yet-closed traces are selectable too.
CLICK_TOLERANCE_PX = 6.0


def _normalize_rect(rect: Rect) -> Rect:
    x0, y0, x1, y1 = rect
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def point_in_rect(point: Point, rect: Rect) -> bool:
    px, py = point
    minx, miny, maxx, maxy = _normalize_rect(rect)
    return minx <= px <= maxx and miny <= py <= maxy


def point_in_polygon(point: Point, polygon: list[Point]) -> bool:
    """Ray-casting point-in-polygon test (True if strictly inside or on an edge
    the ray happens to cross). Needs at least 3 points; else False."""
    n = len(polygon)
    if n < 3:
        return False
    px, py = point
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # Does the horizontal ray at py cross edge (i, j)?
        if (yi > py) != (yj > py):
            x_cross = xi + (py - yi) * (xj - xi) / (yj - yi)
            if px < x_cross:
                inside = not inside
        j = i
    return inside


def _dist_point_to_segment(p: Point, a: Point, b: Point) -> float:
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return hypot(px - (ax + t * dx), py - (ay + t * dy))


def _edges(polygon: list[Point]):
    """Yield boundary segments: consecutive pairs, plus the closing edge when the
    polygon has 3+ points (so a closed ring's last→first side is included)."""
    n = len(polygon)
    for i in range(n - 1):
        yield polygon[i], polygon[i + 1]
    if n >= 3:
        yield polygon[n - 1], polygon[0]


def dist_point_to_polygon(point: Point, polygon: list[Point]) -> float:
    """Smallest distance from *point* to any boundary edge (or the lone vertex of
    a 1-point polygon). ``inf`` for an empty polygon."""
    if not polygon:
        return float("inf")
    if len(polygon) == 1:
        return hypot(point[0] - polygon[0][0], point[1] - polygon[0][1])
    return min(_dist_point_to_segment(point, a, b) for a, b in _edges(polygon))


def point_hits_parcel(point: Point, polygon: list[Point],
                      tol: float = CLICK_TOLERANCE_PX) -> bool:
    """True if a click at *point* selects the parcel: inside a closed boundary,
    or within *tol* pixels of any edge/vertex (so the boundary line itself and
    open/partial traces are clickable too)."""
    if point_in_polygon(point, polygon):
        return True
    return dist_point_to_polygon(point, polygon) <= tol


def _segments_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if v > 0:
            return 1
        if v < 0:
            return -1
        return 0

    def on_seg(a, b, c):  # c collinear with a-b: is it within the segment box?
        return (min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and
                min(a[1], b[1]) <= c[1] <= max(a[1], b[1]))

    o1 = orient(p1, p2, p3)
    o2 = orient(p1, p2, p4)
    o3 = orient(p3, p4, p1)
    o4 = orient(p3, p4, p2)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and on_seg(p1, p2, p3):
        return True
    if o2 == 0 and on_seg(p1, p2, p4):
        return True
    if o3 == 0 and on_seg(p3, p4, p1):
        return True
    if o4 == 0 and on_seg(p3, p4, p2):
        return True
    return False


def polygon_intersects_rect(polygon: list[Point], rect: Rect) -> bool:
    """True if the parcel is *touching or within* the rectangle — any vertex
    inside the rect, the rect fully inside a closed boundary, or any boundary
    edge crossing any rect edge. Matches the brief's marquee semantics."""
    if not polygon:
        return False
    minx, miny, maxx, maxy = _normalize_rect(rect)
    # Any vertex inside the rectangle.
    for pt in polygon:
        if point_in_rect(pt, (minx, miny, maxx, maxy)):
            return True
    corners = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]
    # Rectangle wholly inside a closed parcel.
    if len(polygon) >= 3 and point_in_polygon((minx, miny), polygon):
        return True
    # Any boundary edge crossing any rect edge.
    rect_edges = list(zip(corners, corners[1:] + corners[:1]))
    for a, b in _edges(polygon):
        for c, d in rect_edges:
            if _segments_intersect(a, b, c, d):
                return True
    return False


def parcel_at_point(parcels, point: Point, tol: float = CLICK_TOLERANCE_PX):
    """Return the id of the first parcel a click at *point* hits, or None.
    *parcels* is an ordered list of ``(id, polygon)``; iterated in reverse so the
    last-drawn (topmost) parcel wins when boundaries overlap."""
    for pid, polygon in reversed(list(parcels)):
        if point_hits_parcel(point, polygon, tol):
            return pid
    return None


def parcels_in_rect(parcels, rect: Rect) -> list:
    """Ids of every parcel touching or within *rect*, preserving input order."""
    return [pid for pid, polygon in parcels if polygon_intersects_rect(polygon, rect)]


# --- boundary-edge selection (Milestone 12) --------------------------------
#
# The segment-length report selects a CONTIGUOUS run of a parcel's boundary
# edges. This logic is pure so the canvas stays a thin renderer: it maps a click
# to the nearest edge (below) and asks these functions how the selection changes,
# guaranteeing an unbroken arc by construction (the brief forbids a gap ever
# existing, not merely detecting one afterwards).

def point_segment_distance(p: Point, a: Point, b: Point) -> float:
    """Distance from *p* to segment *a*-*b* (public wrapper over the internal
    helper, so the canvas can hit-test edges in viewport pixels)."""
    return _dist_point_to_segment(p, a, b)


def nearest_edge_index(point: Point, edges, tol: float):
    """Index of the edge (an ``(a, b)`` pair) nearest *point* within *tol*, else
    None. *edges* is an ordered list of segments; ties keep the earliest."""
    best, best_d = None, tol
    for i, (a, b) in enumerate(edges):
        d = _dist_point_to_segment(point, a, b)
        if d <= best_d:
            best_d = d
            best = i
    return best


def contiguous_edge_toggle(selected, edge: int, n_edges: int, closed: bool) -> list:
    """Toggle *edge* within an ordered contiguous run of edge indices, keeping the
    result a single unbroken arc (never a gap). Returns the new ordered run.

    Edges are numbered ``0..n_edges-1``; when *closed*, edge ``n_edges-1`` is
    adjacent to edge ``0`` (the run may wrap). Rules:
      * empty selection -> ``[edge]``;
      * clicking the run's front/back endpoint removes it (shrinks the arc);
      * clicking the lone remaining edge clears the selection;
      * clicking any edge of a full closed loop drops it, leaving a contiguous arc;
      * clicking an edge adjacent to either end extends the run there;
      * anything else (a non-adjacent edge, or an interior edge whose removal would
        split the run) is ignored — a disconnected selection can't be created.
    """
    s = list(selected)
    if not (0 <= edge < n_edges):
        return s
    if not s:
        return [edge]

    if edge in s:
        if len(s) == 1:
            return []
        if closed and len(s) == n_edges:
            start = (edge + 1) % n_edges
            return [(start + k) % n_edges for k in range(n_edges - 1)]
        if edge == s[0]:
            return s[1:]
        if edge == s[-1]:
            return s[:-1]
        return s  # interior removal would split the arc -> no-op

    front, back = s[0], s[-1]
    prev_front = (front - 1) % n_edges if closed else front - 1
    next_back = (back + 1) % n_edges if closed else back + 1
    if edge == prev_front and (closed or front - 1 >= 0):
        return [edge] + s
    if edge == next_back and (closed or back + 1 < n_edges):
        return s + [edge]
    return s  # non-adjacent -> no-op (selection stays contiguous)
