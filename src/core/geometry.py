"""geometry — segment length, perimeter, and area for a traced polygon.

Pure Python, no UI-framework imports. Works in pixel space and converts to SI
using a metres-per-pixel scale (from ``src.core.scale``). SI only at this stage
— square metres and metres; local unit profiles are Milestone 5.

Conventions:
  * points are ``(x, y)`` in image pixels; image y points downward, which does
    not affect distances or (unsigned) area;
  * a polygon's ``area`` is the shoelace area of the closed ring through the
    points (0 for fewer than 3 points), independent of whether the user has
    explicitly "closed" it;
  * ``perimeter`` is the running open-polyline length while tracing, and
    includes the closing edge (last→first) once the polygon is closed — this
    mirrors the live readout the brief asks for.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import atan2, degrees, hypot

Point = tuple[float, float]


def segment_length(a: Point, b: Point) -> float:
    """Euclidean pixel distance between two points."""
    return hypot(b[0] - a[0], b[1] - a[1])


# --- direction / bearing (Milestone 12: boundary-description report) --------
#
# Convention (chosen for un-georeferenced survey scans, which are conventionally
# drawn North-up): screen "up" — decreasing image y — is North, and bearings
# increase clockwise, 0..360 with 0 = North, 90 = East, 180 = South, 270 = West.
# This is plain 2-D geometry from the traced vertex coordinates; it is NOT the
# georeferenced bearing of M16's location-fixing (which resolves true North from
# real-world reference points) and does not depend on it. Bearing is
# scale-invariant, so it needs no scale — only the *length* half of the report
# does (Scale-first rule).

#: Full-word 8-point compass labels used in the traditional boundary description.
_COMPASS_8 = ("North", "North-east", "East", "South-east",
              "South", "South-west", "West", "North-west")


def bearing_deg(a: Point, b: Point) -> float:
    """Compass bearing from *a* to *b* in degrees, North-up / clockwise (see the
    convention note above). Returns 0.0 for a zero-length (degenerate) edge."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    if dx == 0 and dy == 0:
        return 0.0
    # North component is -dy (image y points down); East component is dx.
    return degrees(atan2(dx, -dy)) % 360.0


def compass_label(bearing: float) -> str:
    """Nearest 8-point compass label ('North', 'North-east', ...) for a bearing."""
    return _COMPASS_8[int(bearing / 45.0 + 0.5) % 8]


def segment_lengths(points: list[Point]) -> list[float]:
    """Lengths of each consecutive segment (the open polyline); empty for <2."""
    return [segment_length(points[i], points[i + 1]) for i in range(len(points) - 1)]


def polyline_length(points: list[Point]) -> float:
    """Total length of the open polyline through the points (no closing edge)."""
    return sum(segment_lengths(points))


def polygon_perimeter(points: list[Point]) -> float:
    """Perimeter of the closed polygon (includes the closing edge).

    Returns 0 for fewer than 2 points; for exactly 2 it is the there-and-back
    length (degenerate, but well-defined). Callers that want the running open
    length while tracing should use :func:`polyline_length` instead.
    """
    if len(points) < 2:
        return 0.0
    return polyline_length(points) + segment_length(points[-1], points[0])


def polygon_area(points: list[Point]) -> float:
    """Unsigned shoelace area of the closed polygon in px^2 (0 for <3 points)."""
    n = len(points)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


@dataclass(frozen=True)
class PolygonMeasurement:
    """A snapshot of a polygon's measurements, in pixels and (if a scale is
    available) in SI. ``*_m`` / ``area_sq_m`` are None when no scale is set."""

    point_count: int
    closed: bool
    metres_per_pixel: float | None

    # Pixel-space (always available)
    segment_lengths_px: list[float]
    perimeter_px: float
    area_px: float

    # SI (None when no scale)
    segment_lengths_m: list[float] | None
    perimeter_m: float | None
    area_sq_m: float | None

    @property
    def has_scale(self) -> bool:
        return self.metres_per_pixel is not None

    @property
    def last_segment_px(self) -> float | None:
        return self.segment_lengths_px[-1] if self.segment_lengths_px else None

    @property
    def last_segment_m(self) -> float | None:
        if self.segment_lengths_m:
            return self.segment_lengths_m[-1]
        return None


def measure_polygon(points: list[Point], metres_per_pixel: float | None = None,
                    closed: bool = False) -> PolygonMeasurement:
    """Compute segment lengths, perimeter, and area for *points*.

    *closed* controls only the perimeter: while tracing (open) the perimeter is
    the running polyline length; once closed it includes the last→first edge.
    Area is always the shoelace area of the implied closed ring.
    """
    if metres_per_pixel is not None and metres_per_pixel <= 0:
        raise ValueError(f"metres_per_pixel must be positive, got {metres_per_pixel}")

    seg_px = segment_lengths(points)
    perim_px = polygon_perimeter(points) if closed else polyline_length(points)
    area_px = polygon_area(points)

    if metres_per_pixel is None:
        seg_m = perim_m = area_m2 = None
    else:
        seg_m = [s * metres_per_pixel for s in seg_px]
        perim_m = perim_px * metres_per_pixel
        area_m2 = area_px * metres_per_pixel * metres_per_pixel

    return PolygonMeasurement(
        point_count=len(points),
        closed=closed,
        metres_per_pixel=metres_per_pixel,
        segment_lengths_px=seg_px,
        perimeter_px=perim_px,
        area_px=area_px,
        segment_lengths_m=seg_m,
        perimeter_m=perim_m,
        area_sq_m=area_m2,
    )
