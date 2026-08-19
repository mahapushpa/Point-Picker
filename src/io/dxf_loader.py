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
from pathlib import Path

import ezdxf
from PIL import Image, ImageDraw

from .raster import RasterImage

#: Entity types we rasterise. Everything else is reported, not drawn.
_SUPPORTED = ("LINE", "LWPOLYLINE", "POLYLINE")

#: Longest side of the rendered raster, in pixels, and the blank border around
#: the drawing. The drawing is scaled to fit; this fixes drawing-units-per-pixel.
DEFAULT_MAX_PX = 1600
DEFAULT_PADDING_PX = 20


@dataclass(frozen=True)
class DxfDrawing:
    """The result of rendering a DXF: the raster plus the metadata needed to offer
    a header-units scale candidate and to sanity-check what was drawn."""

    raster: RasterImage
    insunits: int
    units_per_pixel: float                 # drawing units per rendered pixel
    bbox: tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y), drawing units
    entity_count: int                      # drawable entities actually rendered
    skipped_entity_types: tuple[str, ...]  # present but not rendered (flagged)


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
    (x, y) points), plus a set of the entity types we skipped. LINE becomes a
    two-point polyline; closed LWPOLYLINE/POLYLINE get their first point appended
    so the closing edge is drawn."""
    polylines: list[list[tuple[float, float]]] = []
    skipped: set[str] = set()
    for e in msp:
        dxftype = e.dxftype()
        if dxftype == "LINE":
            polylines.append([(e.dxf.start.x, e.dxf.start.y),
                              (e.dxf.end.x, e.dxf.end.y)])
        elif dxftype == "LWPOLYLINE":
            pts = [(x, y) for x, y in e.get_points("xy")]
            if len(pts) >= 2:
                if e.closed:
                    pts = pts + [pts[0]]
                polylines.append(pts)
        elif dxftype == "POLYLINE":
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
            if len(pts) >= 2:
                if e.is_closed:
                    pts = pts + [pts[0]]
                polylines.append(pts)
        else:
            skipped.add(dxftype)
    return polylines, skipped


def render_dxf(path, *, max_px: int = DEFAULT_MAX_PX,
               padding_px: int = DEFAULT_PADDING_PX) -> DxfDrawing:
    """Render a DXF's line/polyline entities to a RasterImage and return it with
    the header unit and the render scale (drawing-units-per-pixel).

    The drawing is scaled uniformly (aspect preserved) to fit *max_px*, which is
    what fixes drawing-units-per-pixel. Raises ``FileNotFoundError`` for a missing
    file, ``ValueError`` for an unparseable DXF or one with no drawable line/
    polyline geometry (nothing to place a boundary against).
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"DXF not found: {p}")
    try:
        doc = ezdxf.readfile(str(p))
    except (IOError, ezdxf.DXFStructureError) as exc:
        raise ValueError(f"could not read DXF: {exc}") from exc

    insunits = int(doc.header.get("$INSUNITS", 0))
    polylines, skipped = _polylines_from_msp(doc.modelspace())
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

    inner = max(1, int(max_px) - 2 * int(padding_px))
    px_per_unit = inner / extent
    units_per_pixel = 1.0 / px_per_unit
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
    )


def load_dxf_page(path, **kwargs) -> RasterImage:
    """Render a DXF to a RasterImage (the entry point used by
    :func:`src.io.raster.open_raster`). Extra keyword args pass through to
    :func:`render_dxf`; the header/scale metadata it also computes is available by
    calling :func:`render_dxf` directly."""
    return render_dxf(path, **kwargs).raster
