"""scale — real-world scale determination.

Pure Python, no UI-framework imports. Canonical storage is SI: a scale is
expressed as **metres per pixel** of the rendered raster.

Milestone 3 implements only *method 3* from PROJECT_BRIEF.md's four
scale-determination methods: the universal fallback where the user marks two
points a known real-world distance apart. The other three (file metadata,
visible reference content, and cross-validation between methods) are later
milestones and are deliberately not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot

Point = tuple[float, float]

#: Identifier stored alongside a scale so later milestones can cross-check
#: methods and be honest about which one produced a given number.
METHOD_TWO_POINT = "two-point"


def pixel_distance(p1: Point, p2: Point) -> float:
    """Euclidean distance in pixels between two (x, y) points."""
    return hypot(p2[0] - p1[0], p2[1] - p1[1])


@dataclass(frozen=True)
class TwoPointScale:
    """Result of a two-point calibration.

    Carries the inputs as well as the derived factor so the calibration can be
    persisted, redone, or cross-checked later without re-deriving it by hand.
    """

    p1: Point
    p2: Point
    pixel_distance: float
    real_distance_m: float
    metres_per_pixel: float
    method: str = METHOD_TWO_POINT

    @property
    def pixels_per_metre(self) -> float:
        return 1.0 / self.metres_per_pixel


def compute_two_point_scale(p1: Point, p2: Point, real_distance_m: float) -> TwoPointScale:
    """Derive metres-per-pixel from two pixel points and the real distance
    between them, in metres.

    Raises ``ValueError`` if the distance is not positive or the two points
    coincide (which would make the scale undefined / infinite).
    """
    if real_distance_m <= 0:
        raise ValueError(f"real-world distance must be positive, got {real_distance_m}")
    px = pixel_distance(p1, p2)
    if px <= 0:
        raise ValueError("the two points coincide (zero pixel distance); pick two distinct points")
    return TwoPointScale(
        p1=(float(p1[0]), float(p1[1])),
        p2=(float(p2[0]), float(p2[1])),
        pixel_distance=px,
        real_distance_m=float(real_distance_m),
        metres_per_pixel=real_distance_m / px,
    )
