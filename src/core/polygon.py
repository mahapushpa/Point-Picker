"""polygon — vertex / boundary topology helpers.

Pure Python, no UI-framework imports. Holds the snap rule shared by the canvas
(live snapping while tracing) and project_db (dedup when persisting and when
migrating), so the tolerance and matching logic live in exactly one testable
place (Milestone 6: topology-aware shared parcel boundaries).

A parcel's boundary is stored as an ordered list of references to *vertices*
that belong to the source; a vertex is shared when several parcels reference it,
which is how adjacent khasras structurally share an edge.
"""

from __future__ import annotations

from math import hypot

Point = tuple[float, float]

#: How close (in rendered-image pixels) a new boundary point must be to an
#: existing vertex to snap to and reuse it instead of creating a new one.
SNAP_TOLERANCE_PX = 8.0


def nearest_vertex_index(point: Point, vertices, tol: float = SNAP_TOLERANCE_PX,
                        exclude_ids=None) -> int | None:
    """Index into *vertices* of the nearest snap candidate to *point*, or None.

    *vertices* is a sequence of ``(vertex_id, x, y)``. A candidate matches only
    if it is within *tol* pixels **and** its id is not in *exclude_ids*.

    Excluding ids is essential: the caller passes the vertices already used by
    the *same* parcel, so a new point never snaps onto one of that parcel's own
    corners (which would silently weld two distinct corners into one and corrupt
    its shape). Snapping may still reuse a *different* parcel's vertex, or an
    unclaimed one — that is exactly the shared-edge case we want.
    """
    exclude = set(exclude_ids) if exclude_ids else frozenset()
    px, py = point
    best_idx: int | None = None
    best_d: float | None = None
    for i, (vid, x, y) in enumerate(vertices):
        if vid in exclude:
            continue
        d = hypot(x - px, y - py)
        if d <= tol and (best_d is None or d < best_d):
            best_d = d
            best_idx = i
    return best_idx
