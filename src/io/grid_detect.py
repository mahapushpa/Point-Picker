"""grid_detect — ruled-grid spacing detection (Milestone 19).

Pure Python, no UI-framework imports. Detects a ruled reference grid on a scanned
image by peak-detection on pixel-darkness sums across rows and columns — the exact
technique the brief names as having worked in the source project. It yields only
the grid *pixel spacing*; the real-world grid interval (metres per cell) is entered
by the user, giving a scale candidate ``mpp = interval / spacing`` (offered, never
auto-applied — the same pattern as every other scale method).

Why ``io/`` not ``core/``: like :mod:`src.io.guardrails` / :mod:`src.io.preprocess`
/ :mod:`src.io.tracing_assist`, this is a heuristic over a source's *decoded raster
pixels* (a :class:`~src.io.raster.RasterImage`). ``io/`` owns the pixel/byte
representation; ``core/`` holds source-independent domain logic (the grid→scale
arithmetic and the cross-check live in ``core.scale``).

Deliberately NOT built (documented, per the brief's allowance to scope down):
detecting a printed **scale bar** or reading a **written scale note** ("1 cm =
40 m"). Both need OCR / shape recognition on mixed-script, often-handwritten
scans — the same class of problem the project already deferred as unreliable
(M10's OCR note). A wrong auto-read scale is worse than none; grid peak-detection
is the one method here robust enough to offer.

**"No clear grid found" is intentionally strict** (a confirmed design decision):
a grid is reported ONLY when BOTH the row and column profiles independently show a
regular peak train AND their spacings agree. A one-directional darkness-peak
pattern (text lines, table rows, a page border) is not necessarily a grid, so we
stay quiet rather than offer a false candidate — manual entry (M3) is always the
working fallback, and a false scale candidate erodes trust more than a missed one.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .raster import RasterImage

#: A peak in a darkness-sum profile must exceed median + PROM_K * (robust spread),
#: where spread = 1.4826*MAD. Grid lines are strong outliers above the many blank
#: rows/columns; a MAD threshold is robust to that blank-majority.
PROM_K = 3.0
#: Minimum number of detected peaks for an axis to be considered a ruled train.
MIN_PEAKS = 5
#: Maximum coefficient of variation (std/mean) of consecutive peak spacings for the
#: train to count as *regular* — a real grid is near-uniform; text/noise is not.
CV_MAX = 0.15
#: Peaks closer than this (px) are merged (a thick line isn't two lines).
MIN_SPACING_PX = 6
#: Row- and column-derived spacings must agree within this fraction to be a grid.
AXIS_AGREE_TOL = 0.10


@dataclass(frozen=True)
class GridDetection:
    """Outcome of grid detection. ``found`` is True only under the strict
    both-axes-agree rule; when False, ``reason`` says plainly why (for an honest
    'no clear grid' message)."""

    found: bool
    spacing_px: float | None            # combined grid spacing when found
    row_spacing_px: float | None        # spacing of horizontal lines (rows), if any
    col_spacing_px: float | None        # spacing of vertical lines (cols), if any
    row_peaks: int
    col_peaks: int
    reason: str


def _darkness(raster: RasterImage) -> np.ndarray:
    if raster.mode != "RGBA":
        raise ValueError(f"grid_detect expects RGBA rasters, got mode {raster.mode!r}")
    arr = np.frombuffer(raster.data, dtype=np.uint8).reshape(raster.height, raster.width, 4)
    gray = arr[:, :, :3].astype(np.float64) @ (0.299, 0.587, 0.114)
    return 255.0 - gray


def _peak_centers(profile: np.ndarray, threshold: float) -> list[float]:
    """Centres of contiguous runs above *threshold*. Using runs (not per-pixel
    local maxima) makes a line of any thickness count once, and a flat profile
    above threshold collapse to a single run — so it can't masquerade as a train."""
    centers: list[float] = []
    n = len(profile)
    i = 0
    while i < n:
        if profile[i] > threshold:
            j = i
            while j < n and profile[j] > threshold:
                j += 1
            centers.append((i + j - 1) / 2.0)
            i = j
        else:
            i += 1
    return centers


def _axis_threshold(profile: np.ndarray) -> float | None:
    """A darkness level clearly above the blank baseline. Robust to a sparse binary
    profile (mostly-zero blanks with strong line spikes, where MAD is 0): fall back
    to halfway between the baseline and the tallest peak. None if the profile is
    flat (no bright structure to threshold)."""
    med = float(np.median(profile))
    mx = float(profile.max())
    if mx <= med:
        return None                          # flat / no ruled structure
    spread = 1.4826 * float(np.median(np.abs(profile - med)))
    # Whichever is higher: the robust outlier level, or halfway to the peak (the
    # latter carries the sparse case where spread == 0).
    return max(med + PROM_K * spread, med + 0.5 * (mx - med))


def _axis_spacing(profile: np.ndarray):
    """(spacing_px | None, n_peaks) for one darkness-sum profile. Returns a spacing
    only for a regular peak train (>= MIN_PEAKS peaks, spacing CV <= CV_MAX)."""
    threshold = _axis_threshold(profile)
    if threshold is None:
        return None, 0
    peaks = _peak_centers(profile, threshold)
    if len(peaks) < MIN_PEAKS:
        return None, len(peaks)
    diffs = np.diff(peaks).astype(np.float64)
    mean = float(diffs.mean())
    if mean < MIN_SPACING_PX:
        return None, len(peaks)              # implausibly dense: not a ruled grid
    cv = float(diffs.std()) / mean
    if cv > CV_MAX:
        return None, len(peaks)              # irregular: not a ruled grid
    return float(np.median(diffs)), len(peaks)


def detect_grid_spacing(raster: RasterImage) -> GridDetection:
    """Detect a ruled grid's pixel spacing on an image. Strict: a grid is reported
    only when both axes show a regular peak train AND their spacings agree within
    :data:`AXIS_AGREE_TOL`. Otherwise ``found`` is False with a plain reason."""
    dark = _darkness(raster)
    row_profile = dark.sum(axis=1)           # horizontal lines -> peaks along y
    col_profile = dark.sum(axis=0)           # vertical lines   -> peaks along x
    rs, rn = _axis_spacing(row_profile)
    cs, cn = _axis_spacing(col_profile)

    if rs is not None and cs is not None:
        if abs(rs - cs) / ((rs + cs) / 2.0) <= AXIS_AGREE_TOL:
            return GridDetection(
                found=True, spacing_px=(rs + cs) / 2.0,
                row_spacing_px=rs, col_spacing_px=cs, row_peaks=rn, col_peaks=cn,
                reason=f"grid found: rows every {rs:.1f}px, cols every {cs:.1f}px")
        return GridDetection(
            found=False, spacing_px=None, row_spacing_px=rs, col_spacing_px=cs,
            row_peaks=rn, col_peaks=cn,
            reason=(f"row spacing ({rs:.1f}px) and column spacing ({cs:.1f}px) "
                    "disagree — not a consistent grid"))

    # One or neither axis is a regular train: not a grid we trust.
    have = []
    if rs is not None:
        have.append("rows")
    if cs is not None:
        have.append("columns")
    detail = (f"only {have[0]} showed a regular ruled pattern" if have
              else "no regular ruled pattern in either direction")
    return GridDetection(
        found=False, spacing_px=None, row_spacing_px=rs, col_spacing_px=cs,
        row_peaks=rn, col_peaks=cn,
        reason=f"no clear grid: {detail}")
