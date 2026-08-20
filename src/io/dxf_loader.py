"""dxf_loader — open a DXF (not DWG) and render its entities to a RasterImage.

Pure Python, no UI-framework imports. Uses ``ezdxf`` to parse the file and
``Pillow`` (already a project dependency) to rasterise — deliberately **no**
matplotlib/Qt drawing backend, since basic line/polyline rendering is all this
milestone needs and adding a heavy render dependency was explicitly held off.

Two things come out of a DXF, mirroring the PDF pattern:
  * a :class:`~src.io.raster.RasterImage` so the whole downstream pipeline
    (canvas, tracing, scale, reports) works on a DXF source **unmodified**, via
    the same neutral contract established in Milestone 2;
  * the header unit (``$INSUNITS``) and the render's drawing-units-per-pixel, the
    raw inputs for a header-derived scale candidate (offered/cross-checked in the
    UI exactly like the PDF-metadata candidate of Milestone 14 — never applied
    silently).

**Deliberately limited entity support (Milestone 15):** only LINE, LWPOLYLINE and
2-D POLYLINE are rendered — enough to show a boundary drawing recognisably. Every
other entity type present (ARC, CIRCLE, SPLINE, TEXT/MTEXT, HATCH, INSERT/blocks,
DIMENSION, …) is counted and reported in ``skipped_entity_types`` rather than
silently dropped, so it is always clear what was not drawn. Curved/So-far
-unsupported geometry is a later concern, not silently approximated here.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from pathlib import Path

import ezdxf
from PIL import Image, ImageDraw

from ..core.scale import metres_per_unit_from_insunits
from .raster import RasterImage

#: Entity types we rasterise. Everything else is reported, not drawn.
_SUPPORTED = ("LINE", "LWPOLYLINE", "POLYLINE")

#: Blank border (px) around the drawing in the rendered raster.
DEFAULT_PADDING_PX = 20

# --- extent-aware render resolution ---------------------------------------
#
# The render resolution is chosen from the drawing's *real-world* extent so that
# a pixel represents a small enough real distance for precise corner tracing —
# rather than a fixed pixel ceiling, which would render a village-scale sheet far
# too coarsely (the same precision concern as PDF render DPI). We target a
# metres-per-pixel figure and compute the pixels needed to hit it, clamped to a
# memory-sane ceiling; if even the ceiling can't reach the target, that is
# reported (precision_limited) rather than silently rendering coarse.

#: Target render precision: real-world metres per pixel. 0.05 m (5 cm) keeps
#: rasterisation error well below manual click/line-width error at parcel-corner
#: scale, so tracing precision is limited by the user, not the raster — while
#: staying far finer than the ~1% area agreement the brief treats as trustworthy.
TARGET_M_PER_PX = 0.05

#: Floor for the longest side (px): small drawings still render large enough to
#: trace comfortably (and end up finer than the target, which is fine).
MIN_MAX_PX = 1600

#: Memory-sane ceiling for the longest side (px). 8000 px longest side is ~256 MB
#: RGBA only in the pathological square-huge case; typical wide sheets are much
#: less. A drawing large enough to need more than this triggers precision_limited.
MAX_MAX_PX = 8000

#: Longest side (px) used when the DXF header carries no real-world unit, so a
#: metres-per-pixel target is meaningless — a plain reasonable default.
UNITLESS_MAX_PX = 2000


@dataclass(frozen=True)
class DxfDrawing:
    """The result of rendering a DXF: the raster plus the metadata needed to offer
    a header-units scale candidate, judge render precision, and sanity-check what
    was drawn."""

    raster: RasterImage
    insunits: int
    units_per_pixel: float                 # drawing units per rendered pixel
    bbox: tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y), drawing units
    entity_count: int                      # drawable entities actually rendered
    skipped_entity_types: tuple[str, ...]  # present but not rendered (flagged)
    metres_per_pixel: float | None         # real-world m/px (None if unitless header)
    precision_target_m: float              # the m/px we aimed for
    precision_limited: bool                # True: extent too large to hit the target
    has_curved_segments: bool = False      # True: a polyline arc (bulge) drawn as a straight chord


def read_dxf_header_units(path) -> int:
    """The DXF ``$INSUNITS`` header code (0 = unitless). Pure metadata read — does
    not render. Raises ``FileNotFoundError`` if the file is missing, ``ValueError``
    if it can't be parsed as a DXF."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"DXF not found: {p}")
    try:
        doc = ezdxf.readfile(str(p))
    except (IOError, ezdxf.DXFStructureError) as exc:
        raise ValueError(f"could not read DXF: {exc}") from exc
    return int(doc.header.get("$INSUNITS", 0))


def _polylines_from_msp(msp):
    """Collect drawable entities as world-coordinate polylines (each a list of
    (x, y) points), a set of the entity types we skipped, and whether any drawn
    polyline carried curvature (a non-zero *bulge* on an LWPOLYLINE/POLYLINE
    segment) that we render as a straight chord. LINE becomes a two-point
    polyline; closed LWPOLYLINE/POLYLINE get their first point appended so the
    closing edge is drawn.

    Bulge matters because an LWPOLYLINE/POLYLINE is a *supported* type, so a
    curved segment inside one would otherwise be silently straightened without
    showing up in *skipped* — a wrong-but-plausible boundary. We flag it instead
    (arc-to-line approximation is a later concern, not silently done here)."""
    polylines: list[list[tuple[float, float]]] = []
    skipped: set[str] = set()
    has_curves = False
    for e in msp:
        dxftype = e.dxftype()
        if dxftype == "LINE":
            polylines.append([(e.dxf.start.x, e.dxf.start.y),
                              (e.dxf.end.x, e.dxf.end.y)])
        elif dxftype == "LWPOLYLINE":
            # "xyb" yields (x, y, bulge); a non-zero bulge means that segment is
            # an arc, drawn here as a straight chord between its endpoints.
            raw = list(e.get_points("xyb"))
            pts = [(x, y) for x, y, _b in raw]
            if any(b for _x, _y, b in raw):
                has_curves = True
            if len(pts) >= 2:
                if e.closed:
                    pts = pts + [pts[0]]
                polylines.append(pts)
        elif dxftype == "POLYLINE":
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
            if any(getattr(v.dxf, "bulge", 0) for v in e.vertices):
                has_curves = True
            if len(pts) >= 2:
                if e.is_closed:
                    pts = pts + [pts[0]]
                polylines.append(pts)
        else:
            skipped.add(dxftype)
    return polylines, skipped, has_curves


def _metres_per_unit_or_none(insunits: int) -> float | None:
    """metres-per-unit for a header code, or None when it carries no real unit."""
    try:
        return metres_per_unit_from_insunits(insunits)
    except ValueError:
        return None


def _auto_max_px(extent_units: float, metres_per_unit: float | None, padding_px: int) -> int:
    """Longest-side pixel count that hits :data:`TARGET_M_PER_PX` for this drawing's
    real-world extent, clamped to [MIN_MAX_PX, MAX_MAX_PX]. Falls back to a fixed
    default when the header has no real unit (no metres target possible)."""
    if metres_per_unit is None:
        return UNITLESS_MAX_PX
    extent_m = extent_units * metres_per_unit
    needed_inner = extent_m / TARGET_M_PER_PX          # pixels across the drawing
    needed = ceil(needed_inner) + 2 * padding_px
    return max(MIN_MAX_PX, min(MAX_MAX_PX, needed))


def render_dxf(path, *, max_px: int | None = None,
               padding_px: int = DEFAULT_PADDING_PX) -> DxfDrawing:
    """Render a DXF's line/polyline entities to a RasterImage and return it with
    the header unit, the render scale (drawing-units-per-pixel), and a real-world
    precision assessment.

    Resolution is **extent-aware** by default (``max_px=None``): it is chosen so a
    pixel spans about :data:`TARGET_M_PER_PX` of real distance, clamped to a
    memory-sane ceiling. If the drawing is so large that even the ceiling can't
    reach that precision, ``precision_limited`` is set (honest about coarseness
    rather than hiding it). Pass an explicit *max_px* to force a specific longest
    side. Raises ``FileNotFoundError`` for a missing file, ``ValueError`` for an
    unparseable DXF or one with no drawable line/polyline geometry.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"DXF not found: {p}")
    try:
        doc = ezdxf.readfile(str(p))
    except (IOError, ezdxf.DXFStructureError) as exc:
        raise ValueError(f"could not read DXF: {exc}") from exc

    insunits = int(doc.header.get("$INSUNITS", 0))
    polylines, skipped, has_curves = _polylines_from_msp(doc.modelspace())
    if not polylines:
        raise ValueError(
            "DXF has no LINE/LWPOLYLINE/POLYLINE entities to render"
            + (f" (only unsupported types: {', '.join(sorted(skipped))})" if skipped else ""))

    xs = [x for pl in polylines for x, _ in pl]
    ys = [y for pl in polylines for _, y in pl]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width_u = max_x - min_x
    height_u = max_y - min_y
    extent = max(width_u, height_u)
    if extent <= 0:
        raise ValueError("DXF entities have zero extent (all coincident); nothing to render")

    metres_per_unit = _metres_per_unit_or_none(insunits)
    if max_px is None:
        max_px = _auto_max_px(extent, metres_per_unit, padding_px)
    else:
        max_px = int(max_px)

    inner = max(1, int(max_px) - 2 * int(padding_px))
    px_per_unit = inner / extent
    units_per_pixel = 1.0 / px_per_unit
    metres_per_pixel = (metres_per_unit * units_per_pixel) if metres_per_unit is not None else None
    # Coarser than the target = precision limited (huge extent at the ceiling, or a
    # small forced max_px). Unknown-unit drawings can't be judged, so never flagged.
    precision_limited = metres_per_pixel is not None and metres_per_pixel > TARGET_M_PER_PX
    img_w = max(1, round(width_u * px_per_unit)) + 2 * padding_px
    img_h = max(1, round(height_u * px_per_unit)) + 2 * padding_px

    def to_px(x, y):
        # World Y is up; image Y is down — flip. Left/top padded by padding_px.
        return (padding_px + (x - min_x) * px_per_unit,
                padding_px + (max_y - y) * px_per_unit)

    img = Image.new("RGBA", (img_w, img_h), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)
    for pl in polylines:
        draw.line([to_px(x, y) for x, y in pl], fill=(0, 0, 0, 255), width=1)

    raster = RasterImage(width=img_w, height=img_h, data=img.tobytes(), mode="RGBA")
    return DxfDrawing(
        raster=raster,
        insunits=insunits,
        units_per_pixel=units_per_pixel,
        bbox=(min_x, min_y, max_x, max_y),
        entity_count=len(polylines),
        skipped_entity_types=tuple(sorted(skipped)),
        metres_per_pixel=metres_per_pixel,
        precision_target_m=TARGET_M_PER_PX,
        precision_limited=precision_limited,
        has_curved_segments=has_curves,
    )


def load_dxf_page(path, **kwargs) -> RasterImage:
    """Render a DXF to a RasterImage (the entry point used by
    :func:`src.io.raster.open_raster`). Extra keyword args pass through to
    :func:`render_dxf`; the header/scale metadata it also computes is available by
    calling :func:`render_dxf` directly."""
    return render_dxf(path, **kwargs).raster
