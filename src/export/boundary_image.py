"""boundary_image — crop the source raster around a parcel's traced boundary.

Pure Python, no UI-framework imports (it lives in the export layer and may use
``src.io``, which is also UI-free). Produces a small image of just the parcel's
neighbourhood — the source scan cropped to the boundary's bounding box plus
padding, with the traced boundary drawn on top — for embedding in that parcel's
report entry (Milestone 11, item 3). This is a per-parcel thumbnail, not the full
toggleable visual-confidence overlay (that is Milestone 13).

Sources load through :func:`src.io.raster.open_raster`, so both raster images and
rendered PDF pages are supported uniformly. A crop whose aspect ratio is too
extreme (or whose size is degenerate) to sit in the PDF layout is flagged for
saving as a separate PNG and referencing instead — never silently omitted.
"""

from __future__ import annotations

import io as _io
from dataclasses import dataclass

from ..io.raster import open_raster

#: A crop wider or taller than this ratio doesn't sit well inline in the PDF.
MAX_ASPECT = 5.0
#: Below this pixel dimension a crop is too small to embed legibly.
MIN_DIM = 6
#: Boundary outline colour (RGBA) drawn on the crop.
_OUTLINE = (230, 60, 10, 255)


@dataclass
class ParcelCrop:
    """The cropped boundary image for one parcel, plus a placement decision.

    Exactly one of *png_bytes* (embed inline) or *external_filename* (saved as a
    separate PNG, reference it) is set when :attr:`available` is True. When a crop
    could not be produced, both are None and *reason* explains why."""
    parcel_id: int
    png_bytes: bytes | None = None
    external_filename: str | None = None
    width: int = 0
    height: int = 0
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.png_bytes is not None or self.external_filename is not None

    @property
    def is_external(self) -> bool:
        return self.external_filename is not None


def _bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def load_source_image(source_path, open_kwargs=None):
    """Decode a source to a full-size RGBA ``PIL.Image`` once. Callers generating
    several crops from the *same* source (e.g. an owner report where many parcels
    share one sheet) should decode once via this and pass the result to
    :func:`render_crop` as *base_image*, rather than re-decoding per parcel."""
    from PIL import Image

    raster = open_raster(source_path, **(open_kwargs or {}))
    return Image.frombytes("RGBA", (raster.width, raster.height), raster.data)


def render_crop(source_path, pixel_points, *, closed=True, padding_frac=0.18,
                min_pad=10, open_kwargs=None, base_image=None):
    """Return a cropped RGBA ``PIL.Image`` around *pixel_points* with the boundary
    drawn on it, or raise if the source cannot be loaded. *open_kwargs* is passed
    to :func:`open_raster` (e.g. ``page=`` for a PDF source).

    Pass *base_image* (a full-size source ``PIL.Image`` from
    :func:`load_source_image`) to reuse an already-decoded source instead of
    reading it from disk again — the crop is a cheap sub-region copy, so this
    turns an N-parcel report from N full decodes into one.
    """
    from PIL import Image, ImageDraw

    if base_image is not None:
        img = base_image
    else:
        img = load_source_image(source_path, open_kwargs)

    minx, miny, maxx, maxy = _bbox(pixel_points)
    pad_x = max(min_pad, (maxx - minx) * padding_frac)
    pad_y = max(min_pad, (maxy - miny) * padding_frac)
    left = max(0, int(minx - pad_x))
    top = max(0, int(miny - pad_y))
    right = min(img.width, int(maxx + pad_x) + 1)
    bottom = min(img.height, int(maxy + pad_y) + 1)
    # Guard against a degenerate (empty) crop box.
    if right <= left:
        right = min(img.width, left + 1)
    if bottom <= top:
        bottom = min(img.height, top + 1)

    crop = img.crop((left, top, right, bottom)).convert("RGBA")
    draw = ImageDraw.Draw(crop)
    local = [(x - left, y - top) for x, y in pixel_points]
    if len(local) >= 2:
        line = local + [local[0]] if closed else local
        draw.line(line, fill=_OUTLINE, width=2)
    for x, y in local:  # small vertex dots so corners are visible
        draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=_OUTLINE)
    return crop


def classify_crop(width, height):
    """Decide whether a crop of this size embeds inline ('embed') or should be a
    separate PNG ('external'), with a human reason for the latter."""
    if width < MIN_DIM or height < MIN_DIM:
        return "external", f"crop too small to embed ({width}x{height} px)"
    aspect = width / height
    if aspect > MAX_ASPECT:
        return "external", f"crop too wide to embed (aspect {aspect:.1f})"
    if aspect < 1.0 / MAX_ASPECT:
        return "external", f"crop too tall to embed (aspect {aspect:.1f})"
    return "embed", None


def to_png_bytes(image) -> bytes:
    buf = _io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
