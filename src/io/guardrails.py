"""guardrails — point-data guard rails (Milestone 17): two WARNING-ONLY checks.

Pure Python, no UI-framework imports. Both checks only *flag* — they never move,
remove, or reject a placed point. The user dismisses or acts on the warning; the
tool never decides for them (the source project's hard lesson: an automated
"is this a real corner or noise" judgement is unreliable, so a dismissible flag
is the right level of automation, not a silent fix).

1. **Likely-accidental duplicate** — a new point placed within the M6 snap
   tolerance of the immediately-preceding one is almost certainly a double-click
   (a deliberate distinct corner would be placed farther than the snap-reuse
   radius). Pure geometry; reuses ``SNAP_TOLERANCE_PX`` rather than inventing a
   new "too close" number. Applies to every source type.

2. **Likely-missing corner** — a traced edge whose straight chord does not track a
   consistent ink/line signal in the underlying scan, suggesting a real bend was
   missed. A deliberately simple, conservative pixel-sampling heuristic (see
   :func:`find_missing_corner_edges`): it stays quiet unless the evidence is
   clear, because a false alarm erodes trust in the warning faster than an
   occasional miss ("prefer honest uncertainty over a confident-looking wrong
   answer"). This is a raster check, so it is meaningful only for scanned/rendered
   *pixel* sources; a DXF renders exact vector lines to a clean raster we generate
   ourselves, with no scan ambiguity to sample, so callers skip #2 for DXF.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot

from ..core.polygon import SNAP_TOLERANCE_PX
from .raster import RasterImage

Point = tuple[float, float]

#: "Too close to be a distinct point" = the same radius M6 uses to decide two
#: marks are the same shared vertex. Reused deliberately (see module docstring).
DUP_TOLERANCE_PX = SNAP_TOLERANCE_PX

# --- missing-corner heuristic defaults (conservative on purpose) -----------
#: Edges shorter than this (px) are never flagged — too short to hide a
#: meaningful missed corner, and short edges are where false alarms cluster.
MIN_EDGE_PX = 40.0
#: Flag only when the interior ink coverage along the chord is clearly low.
COVERAGE_THRESHOLD = 0.6
#: Perpendicular tolerance (px) when asking "is there ink at this sample?" —
#: absorbs line width and small tracing offset.
SEARCH_RADIUS_PX = 3
#: Interior sample spacing (px) along the chord.
SAMPLE_STEP_PX = 2.0
#: Ink threshold = mean - INK_K*std of the grayscale image. Adaptive, and quiet
#: on a blank image (std ~ 0 -> nothing counts as ink -> nothing judged).
INK_K = 1.0


def is_duplicate_point(new: Point, prev: Point,
                       tolerance_px: float = DUP_TOLERANCE_PX) -> bool:
    """True if *new* is implausibly close to *prev* (a probable double-click)."""
    return hypot(new[0] - prev[0], new[1] - prev[1]) <= tolerance_px


@dataclass(frozen=True)
class EdgeWarning:
    """A traced edge that looks like it may skip a real corner."""
    edge_index: int
    a: Point
    b: Point
    length_px: float
    coverage: float   # fraction of interior samples that sit on ink (0..1)


def _edges(points, closed: bool):
    """Edges as ``(edge_index, a, b)`` matching the M12 numbering (closing edge is
    index n-1 when the boundary is closed)."""
    pts = list(points)
    edges = [(i, pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    if closed and len(pts) >= 3:
        edges.append((len(pts) - 1, pts[-1], pts[0]))
    return edges


def _ink_near(ink, pt: Point, radius: int) -> bool:
    """Any ink pixel within *radius* (a box) of *pt*? Out-of-bounds counts as no
    ink."""
    h, w = ink.shape
    x = int(round(pt[0]))
    y = int(round(pt[1]))
    x0, x1 = max(0, x - radius), min(w, x + radius + 1)
    y0, y1 = max(0, y - radius), min(h, y + radius + 1)
    if x0 >= x1 or y0 >= y1:
        return False
    return bool(ink[y0:y1, x0:x1].any())


def _ink_mask(raster: RasterImage, ink_k: float):
    """Boolean HxW ink mask: pixels darker than mean - ink_k*std of the grayscale.
    Adaptive and quiet on a blank image."""
    import numpy as np
    if raster.mode != "RGBA":
        raise ValueError(f"guardrails expects RGBA rasters, got mode {raster.mode!r}")
    arr = np.frombuffer(raster.data, dtype=np.uint8).reshape(raster.height, raster.width, 4)
    gray = arr[:, :, :3].astype(np.float64) @ (0.299, 0.587, 0.114)
    thr = gray.mean() - ink_k * gray.std()
    return gray < thr


def find_missing_corner_edges(raster: RasterImage, points, closed: bool, *,
                              min_edge_px: float = MIN_EDGE_PX,
                              coverage_threshold: float = COVERAGE_THRESHOLD,
                              search_radius: int = SEARCH_RADIUS_PX,
                              sample_step_px: float = SAMPLE_STEP_PX,
                              ink_k: float = INK_K) -> list[EdgeWarning]:
    """Flag traced edges whose straight chord doesn't track ink in the scan.

    Conservative by design — an edge is flagged only when ALL of:
      * it is at least *min_edge_px* long (short edges never flagged);
      * BOTH endpoints sit on ink (so we know a real line exists there to judge
        against — otherwise we can't tell and stay quiet);
      * the fraction of its interior samples sitting on ink is below
        *coverage_threshold* (the straight line mostly crosses blank paper, i.e.
        the real boundary likely bent away and a corner was missed).

    Returns a per-edge list of :class:`EdgeWarning` (empty when nothing is clearly
    suspicious). Never mutates anything. Meant for pixel sources; callers skip DXF.
    """
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 2:
        return []
    ink = _ink_mask(raster, ink_k)
    warnings: list[EdgeWarning] = []
    for idx, a, b in _edges(pts, closed):
        length = hypot(b[0] - a[0], b[1] - a[1])
        if length < min_edge_px:
            continue
        if not (_ink_near(ink, a, search_radius) and _ink_near(ink, b, search_radius)):
            continue   # can't judge without ink at both ends — stay quiet
        n = max(2, int(length / sample_step_px))
        hits = 0
        total = 0
        for k in range(1, n):          # interior samples only (exclude endpoints)
            t = k / n
            p = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
            total += 1
            if _ink_near(ink, p, search_radius):
                hits += 1
        coverage = hits / total if total else 1.0
        if coverage < coverage_threshold:
            warnings.append(EdgeWarning(idx, a, b, length, coverage))
    return warnings
