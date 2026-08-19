"""scale — real-world scale determination.

Pure Python, no UI-framework imports. Canonical storage is SI: a scale is
expressed as **metres per pixel** of the rendered raster.

Milestone 3 implemented *method 3*: the universal fallback where the user marks
two points a known real-world distance apart. Milestone 14 adds *method 1* for
PDF sources — deriving a scale candidate from the page's own physical size — plus
*method 4*, the cross-check that surfaces agreement/disagreement between two
scales rather than silently trusting one. Reading the PDF metadata itself lives
in ``src.io.pdf_loader`` (PyMuPDF); everything here is pure arithmetic on numbers
already extracted, so this module stays free of any file/GUI dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot

Point = tuple[float, float]

#: Identifiers stored alongside a scale so we can cross-check methods and stay
#: honest about which one produced a given number.
METHOD_TWO_POINT = "two-point"
METHOD_PDF_METADATA = "pdf-metadata"

#: Unit conversions for reading a PDF's physical page size. A PDF user-space unit
#: is the point = 1/72 inch; an inch is exactly 0.0254 m.
POINTS_PER_INCH = 72.0
METRES_PER_INCH = 0.0254

#: Plausibility window for a PDF page dimension, in points. Outside it a MediaBox
#: is almost certainly a placeholder or garbage, so we refuse to derive a
#: confident scale from it (honesty over a confident-looking wrong number, per
#: the brief). ~3 pt ≈ 1 mm; 14400 pt = 200 in is the PDF spec's maximum page.
MIN_PAGE_POINTS = 3.0
MAX_PAGE_POINTS = 14400.0


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


# -- method 1: PDF page-metadata scale (Milestone 14) -----------------------


def points_to_metres(points: float) -> float:
    """Convert a length in PDF points (1/72 inch) to metres."""
    return points / POINTS_PER_INCH * METRES_PER_INCH


@dataclass(frozen=True)
class MetadataScale:
    """A scale candidate derived from a PDF page's own physical size (method 1).

    Valid only when the page was generated at true physical (1:1) scale — a vector
    export whose page dimensions are the real-world dimensions. Otherwise it will
    disagree with a manual calibration, which is exactly what the M14 cross-check
    is for. Carries the raw inputs so the figure is always explainable, never a
    bare number.
    """

    width_pt: float
    height_pt: float
    width_m: float
    height_m: float
    raster_width_px: int
    raster_height_px: int
    metres_per_pixel: float
    method: str = METHOD_PDF_METADATA

    @property
    def pixels_per_metre(self) -> float:
        return 1.0 / self.metres_per_pixel


def metadata_scale_from_page(width_pt: float, height_pt: float,
                             raster_width_px: int, raster_height_px: int) -> MetadataScale:
    """Derive metres-per-pixel from a PDF page's physical size (points) and the
    pixel size it was rendered to. The page's physical extent maps onto the
    rendered raster, so one rendered pixel spans ``page_metres / raster_pixels`` of
    real distance — *if the page is at true physical scale*.

    Raises ``ValueError`` if the page size is missing/degenerate/implausible or the
    raster dimensions are non-positive, so callers fall back to manual scale rather
    than trusting a bogus number.
    """
    if raster_width_px <= 0 or raster_height_px <= 0:
        raise ValueError(
            f"raster dimensions must be positive, got {raster_width_px}x{raster_height_px}")
    for name, pts in (("width", width_pt), ("height", height_pt)):
        if not (MIN_PAGE_POINTS <= pts <= MAX_PAGE_POINTS):
            raise ValueError(
                f"implausible PDF page {name} {pts:g} pt (expected "
                f"{MIN_PAGE_POINTS:g}..{MAX_PAGE_POINTS:g} pt); "
                "cannot derive a scale from page metadata")
    width_m = points_to_metres(width_pt)
    height_m = points_to_metres(height_pt)
    # Rendering preserves aspect ratio, so the width- and height-derived values
    # agree; average them to shave rounding.
    mpp = (width_m / raster_width_px + height_m / raster_height_px) / 2.0
    return MetadataScale(
        width_pt=float(width_pt), height_pt=float(height_pt),
        width_m=width_m, height_m=height_m,
        raster_width_px=int(raster_width_px), raster_height_px=int(raster_height_px),
        metres_per_pixel=mpp,
    )


# -- method 4: cross-check between two scales (Milestone 14) -----------------


@dataclass(frozen=True)
class ScaleCrossCheck:
    """Comparison of two independent metres-per-pixel scales for one source:
    surface agreement/disagreement rather than silently pick a winner (method 4)."""

    manual_mpp: float
    metadata_mpp: float
    percent_difference: float
    agree: bool
    tolerance_percent: float

    def describe(self) -> str:
        if self.agree:
            return (f"Manual and PDF-metadata scales agree "
                    f"({self.percent_difference:.1f}% apart, within "
                    f"{self.tolerance_percent:g}%).")
        return (f"Manual and PDF-metadata scales DISAGREE by "
                f"{self.percent_difference:.1f}% (more than {self.tolerance_percent:g}%) — "
                "check which one is right before trusting measurements.")


def compare_scales(manual_mpp: float, metadata_mpp: float, *,
                   tolerance_percent: float = 2.0) -> ScaleCrossCheck:
    """Symmetric percentage difference between two metres-per-pixel scales, and
    whether they agree within *tolerance_percent*. Deliberately simple (per brief:
    a plain percentage difference is enough)."""
    if manual_mpp <= 0 or metadata_mpp <= 0:
        raise ValueError("both scales must be positive to compare")
    mean = (manual_mpp + metadata_mpp) / 2.0
    percent = abs(manual_mpp - metadata_mpp) / mean * 100.0
    return ScaleCrossCheck(
        manual_mpp=float(manual_mpp), metadata_mpp=float(metadata_mpp),
        percent_difference=percent, agree=percent <= tolerance_percent,
        tolerance_percent=float(tolerance_percent),
    )
