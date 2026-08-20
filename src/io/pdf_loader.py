"""pdf_loader — render a PDF page to a raster image.

Pure Python, no UI-framework imports. Uses PyMuPDF (imported as ``pymupdf``,
with a fallback to the legacy ``fitz`` name) to rasterise a page, and returns a
neutral :class:`~src.io.raster.RasterImage` (RGBA bytes) — never a Qt type — so
this stays testable with no GUI running.

Milestone 2 rendered to screen; Milestone 14 adds :func:`read_page_size_points`,
which reads the page's own physical size (its MediaBox, in points) as the raw
input for metadata-based scale detection. That's still a pure metadata read — the
scale arithmetic and plausibility checks live in ``src.core.scale``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .raster import RasterImage

try:  # PyMuPDF >= 1.24 exposes the package as `pymupdf`; older wheels as `fitz`.
    import pymupdf as _fitz
except ImportError:  # pragma: no cover - depends on installed version
    import fitz as _fitz

#: Paper-space render-precision target, in DPI. At render time a scanned survey
#: PDF's *ground* scale is unknown (its physical page size is paper size, not
#: ground extent — unlike a DXF whose coordinates are real-world), so the lever we
#: control here is paper resolution. 150 DPI (~5.9 px/mm) resolves printed survey
#: linework and closely-spaced corners well and is the long-standing default;
#: keeping it as the target means normal pages render byte-identically (existing
#: traced pixel coordinates stay valid). The *ground* precision, once a scale is
#: set, is checked separately (core.scale.is_coarse_scale). This is the analogue
#: of DXF's 0.05 m/px target: a stated goal, capped for memory, flagged when the
#: cap forces a coarser render.
DEFAULT_DPI = 150

#: Memory-sane ceiling for the longest raster side (px), matching the DXF loader's
#: MAX_MAX_PX. 150 DPI only exceeds this past ~53 in (1.35 m) on the long side —
#: i.e. larger than A0 — so ordinary sheets are unaffected and only a genuinely
#: huge sheet is reduced below target and flagged precision_limited.
MAX_RENDER_PX = 8000


@dataclass(frozen=True)
class PdfRenderInfo:
    """Render-precision metadata for a PDF page (parallel to DXF's precision
    fields): the DPI actually used, the target it was measured against, and
    whether the memory cap forced it below target."""

    dpi: float
    target_dpi: int
    precision_limited: bool
    page_width_pt: float
    page_height_pt: float


def _render_dpi_for_page(width_pt: float, height_pt: float, *,
                         target_dpi: int = DEFAULT_DPI,
                         max_px: int = MAX_RENDER_PX) -> tuple[float, bool]:
    """The DPI to render this page at: the target, unless that would exceed the
    longest-side pixel ceiling, in which case the DPI is reduced to fit and the
    call reports ``precision_limited=True``. Deterministic from the page size, so
    every render path produces identical pixels for the same source."""
    longest_in = max(float(width_pt), float(height_pt)) / 72.0
    if longest_in <= 0:
        return float(target_dpi), False
    px_at_target = longest_in * target_dpi
    if px_at_target <= max_px:
        return float(target_dpi), False
    return (max_px / longest_in), True


def pdf_render_info(path, page: int = 0) -> PdfRenderInfo:
    """Report the render DPI and precision assessment for a page, without keeping
    the raster. Used by the UI to flag a very large sheet the same way a large DXF
    is flagged."""
    w_pt, h_pt = read_page_size_points(path, page=page)
    dpi, limited = _render_dpi_for_page(w_pt, h_pt)
    return PdfRenderInfo(dpi=dpi, target_dpi=DEFAULT_DPI, precision_limited=limited,
                         page_width_pt=w_pt, page_height_pt=h_pt)


def page_count(path) -> int:
    """Number of pages in the PDF at *path*."""
    with _fitz.open(str(path)) as doc:
        return doc.page_count


def read_page_size_points(path, page: int = 0) -> tuple[float, float]:
    """The physical size of a PDF page in points (1/72 inch) as ``(width, height)``.

    This is the page's own laid-out rectangle (rotation applied) — the raw input
    for metadata-based scale detection (Milestone 14). A pure metadata read; it
    renders nothing. Raises ``FileNotFoundError`` if the file is missing and
    ``ValueError`` for an out-of-range page.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"PDF not found: {p}")
    with _fitz.open(str(p)) as doc:
        if page < 0 or page >= doc.page_count:
            raise ValueError(f"page {page} out of range (document has {doc.page_count} page(s))")
        rect = doc.load_page(page).rect
        return (float(rect.width), float(rect.height))


def extract_text(path) -> str:
    """Return the concatenated text layer of a PDF (all pages), or ``""`` if it
    has none (a scan). Pure extraction — no OCR, no guessing — for the optional
    reference-document panel (C8), where the user copies exact values by hand.
    Raises ``FileNotFoundError`` if the file is missing, ``ValueError`` if it
    can't be opened as a PDF."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"PDF not found: {p}")
    try:
        with _fitz.open(str(p)) as doc:
            parts = [doc.load_page(i).get_text("text") for i in range(doc.page_count)]
    except Exception as exc:  # noqa: BLE001 — surface as a clean ValueError
        raise ValueError(f"could not read PDF text: {exc}") from exc
    return "\n".join(parts).strip()


def load_pdf_page(path, page: int = 0, dpi: int | None = None) -> RasterImage:
    """Render one page of a PDF to an RGBA :class:`RasterImage`.

    *page* is 0-based. *dpi* controls render resolution; when ``None`` (the
    default) it is chosen adaptively — the :data:`DEFAULT_DPI` target, reduced
    only if the page is so physically large that the target would exceed
    :data:`MAX_RENDER_PX` on the longest side. The choice is deterministic from
    the page size, so every render path (display, report crops) produces
    identical pixels for the same source — keeping stored pixel coordinates valid.
    Raises ``FileNotFoundError`` if the file is missing, ``ValueError`` for an
    out-of-range page.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"PDF not found: {p}")

    with _fitz.open(str(p)) as doc:
        if page < 0 or page >= doc.page_count:
            raise ValueError(f"page {page} out of range (document has {doc.page_count} page(s))")
        pdf_page = doc.load_page(page)
        if dpi is None:
            rect = pdf_page.rect
            dpi, _limited = _render_dpi_for_page(rect.width, rect.height)
        # alpha=True yields 4 channels (RGBA), matching RasterImage's contract
        # and Qt's Format_RGBA8888 without any per-row padding.
        pix = pdf_page.get_pixmap(dpi=int(round(dpi)), alpha=True)
        return RasterImage(
            width=pix.width,
            height=pix.height,
            data=bytes(pix.samples),
            mode="RGBA",
        )
