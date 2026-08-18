"""pdf_loader — render a PDF page to a raster image.

Pure Python, no UI-framework imports. Uses PyMuPDF (imported as ``pymupdf``,
with a fallback to the legacy ``fitz`` name) to rasterise a page, and returns a
neutral :class:`~src.io.raster.RasterImage` (RGBA bytes) — never a Qt type — so
this stays testable with no GUI running.

Reading the PDF's own physical page size as a scale clue is a Milestone 8
concern and is deliberately not done here; Milestone 2 only renders to screen.
"""

from __future__ import annotations

from pathlib import Path

from .raster import RasterImage

try:  # PyMuPDF >= 1.24 exposes the package as `pymupdf`; older wheels as `fitz`.
    import pymupdf as _fitz
except ImportError:  # pragma: no cover - depends on installed version
    import fitz as _fitz

#: Default render resolution. Screen display doesn't need print DPI; 150 is a
#: good balance of sharpness and memory for large survey sheets.
DEFAULT_DPI = 150


def page_count(path) -> int:
    """Number of pages in the PDF at *path*."""
    with _fitz.open(str(path)) as doc:
        return doc.page_count


def load_pdf_page(path, page: int = 0, dpi: int = DEFAULT_DPI) -> RasterImage:
    """Render one page of a PDF to an RGBA :class:`RasterImage`.

    *page* is 0-based. *dpi* controls render resolution. Raises ``FileNotFoundError``
    if the file is missing, ``ValueError`` for an out-of-range page.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"PDF not found: {p}")

    with _fitz.open(str(p)) as doc:
        if page < 0 or page >= doc.page_count:
            raise ValueError(f"page {page} out of range (document has {doc.page_count} page(s))")
        pdf_page = doc.load_page(page)
        # alpha=True yields 4 channels (RGBA), matching RasterImage's contract
        # and Qt's Format_RGBA8888 without any per-row padding.
        pix = pdf_page.get_pixmap(dpi=dpi, alpha=True)
        return RasterImage(
            width=pix.width,
            height=pix.height,
            data=bytes(pix.samples),
            mode="RGBA",
        )
