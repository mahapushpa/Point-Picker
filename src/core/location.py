"""location — local georeferencing / location-fixing (Milestone 16).

Pure Python, no UI imports. Distance/trigonometry mode (mode 1) only; the
GPS-anchored mode (mode 2) is documented in PROJECT_BRIEF.md but deliberately not
built here.

Solves for *position* the way :mod:`src.core.scale` solves for *size*: from
durable reference landmarks marked on the sheet, express where a parcel boundary
point sits — as a real-world distance + bearing from each landmark — and
cross-check several landmarks against one another.

Two ways an observation is formed (both supported, per the M16 design decision):
  * **SHEET** — the landmark and the target are both marked on the sheet; the
    distance and bearing are computed from the pixel geometry and the source
    scale. This is the "38 m from the tubewell, bearing 42 deg" description.
  * **FIELD** — only the landmark is marked; the user supplies a field-measured
    distance (and bearing, if known); the target position is computed by
    trigonometry. This is the brief's primary framing, and the one that lets a
    tape-and-compass field measurement be compared against where the sheet places
    the boundary (encroachment).

Bearing uses the SAME convention as the boundary-description report (M12):
screen-up = North, clockwise, 0..360 (see :func:`src.core.geometry.bearing_deg`).
It is the sheet's own up direction (a scan's / PDF page's / DXF drawing's top),
NOT guaranteed true north — the M12 caveat applies unchanged. Distances need the
per-source scale (metres per pixel); nothing here holds a scale of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, hypot, radians, sin

from .geometry import bearing_deg, compass_label

Point = tuple[float, float]

#: How an observation's distance/bearing were obtained (stored per fix).
SOURCE_SHEET = "sheet"
SOURCE_FIELD = "field"

#: Default agreement tolerance (metres) for cross-validating implied positions.
#: A few metres reflects the field-reproducible-with-tape-and-compass accuracy the
#: brief targets (not geodetic precision). Configurable per call.
DEFAULT_TOLERANCE_M = 3.0


def observation_from_points(reference: Point, target: Point,
                            metres_per_pixel: float) -> tuple[float, float]:
    """(distance_m, bearing_deg) from a landmark to a target — both in sheet
    pixels — using the source scale. Bearing is M12's screen-up-North convention.
    Raises ``ValueError`` without a positive scale (result is meaningless)."""
    if metres_per_pixel <= 0:
        raise ValueError("a positive metres-per-pixel scale is required")
    d_px = hypot(target[0] - reference[0], target[1] - reference[1])
    return d_px * metres_per_pixel, bearing_deg(reference, target)


def target_from_field(reference: Point, distance_m: float, bearing: float,
                      metres_per_pixel: float) -> Point:
    """Target pixel implied by a field distance + bearing from a landmark — the
    inverse of :func:`observation_from_points`. Bearing in M12 convention
    (0 = screen-up / North, clockwise)."""
    if metres_per_pixel <= 0:
        raise ValueError("a positive metres-per-pixel scale is required")
    if distance_m < 0:
        raise ValueError("distance must be non-negative")
    d_px = distance_m / metres_per_pixel
    th = radians(bearing)
    # East = +x = d*sin(th); North = -y = d*cos(th)  ->  y-component = -d*cos(th).
    return (reference[0] + d_px * sin(th), reference[1] - d_px * cos(th))


def format_description(label: str, distance_m: float, bearing: float | None) -> str:
    """ASCII-only one-liner, e.g. ``'38.0 m from tubewell, bearing 42 deg
    (North-east)'``. Uses `` deg`` (not the degree sign) so it survives PyMuPDF's
    WinAnsi PDF font, the M11/M12 lesson."""
    where = label.strip() or "reference"
    if bearing is None:
        return f"{distance_m:.1f} m from {where} (bearing unknown)"
    return (f"{distance_m:.1f} m from {where}, bearing {bearing:.0f} deg "
            f"({compass_label(bearing)})")


@dataclass(frozen=True)
class LocationCrossCheck:
    """Result of comparing the positions several references imply for one target
    (method-4 style: surface disagreement, never silently pick one)."""

    n: int                              # positions compared
    spread_m: float                     # max pairwise separation, metres
    tolerance_m: float
    agree: bool
    centroid: Point | None              # mean implied position (pixels)
    deviations_m: tuple[float, ...]     # each position's distance from the centroid

    def describe(self) -> str:
        if self.n == 0:
            return "No reference positions to cross-check."
        if self.n == 1:
            return ("Single reference only — position is unverified; add 2+ more "
                    "landmarks to cross-check it.")
        if self.agree:
            return (f"{self.n} references agree: implied positions within "
                    f"{self.spread_m:.1f} m (tolerance {self.tolerance_m:g} m).")
        return (f"{self.n} references DISAGREE: implied positions spread "
                f"{self.spread_m:.1f} m (more than {self.tolerance_m:g} m) — "
                "check the landmarks/measurements before trusting the location.")


def cross_validate_positions(points: list[Point], metres_per_pixel: float, *,
                             tolerance_m: float = DEFAULT_TOLERANCE_M) -> LocationCrossCheck:
    """Compare the target positions implied by several references. *points* are the
    per-reference implied/target pixels; the spread (max pairwise separation) is
    converted to metres and flagged against *tolerance_m*. Fewer than two positions
    can't be cross-checked (reported honestly as unverified)."""
    if metres_per_pixel <= 0:
        raise ValueError("a positive metres-per-pixel scale is required")
    n = len(points)
    if n == 0:
        return LocationCrossCheck(0, 0.0, tolerance_m, True, None, ())
    cx = sum(p[0] for p in points) / n
    cy = sum(p[1] for p in points) / n
    deviations_m = tuple(hypot(p[0] - cx, p[1] - cy) * metres_per_pixel for p in points)
    spread_m = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            d = hypot(points[i][0] - points[j][0], points[i][1] - points[j][1]) * metres_per_pixel
            spread_m = max(spread_m, d)
    agree = n < 2 or spread_m <= tolerance_m
    return LocationCrossCheck(n, spread_m, tolerance_m, agree, (cx, cy), deviations_m)
