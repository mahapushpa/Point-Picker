"""raster — the neutral in-memory image type returned by every loader.

Pure Python, no UI-framework imports. This is the contract between io/ (which
decodes files) and ui/ (which draws them): loaders hand back a RasterImage —
plain width/height/mode/bytes — and the Qt layer turns that into a QImage. No
Qt type ever crosses into io/, which is what keeps the loaders testable with no
GUI running and reusable behind a different UI in Phase 2.

Loaders always produce 8-bit RGBA (mode ``"RGBA"``, 4 bytes/pixel, no row
padding: ``len(data) == width * height * 4``). RGBA is chosen because it maps
one-to-one onto Qt's ``Format_RGBA8888`` with a naturally 32-bit-aligned stride,
sidestepping the per-row padding pitfalls of packed RGB.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RasterImage:
    """A decoded raster image as raw pixel bytes, independent of any toolkit."""

    width: int
    height: int
    data: bytes
    mode: str = "RGBA"  # currently always RGBA; kept explicit for the Qt side

    def __post_init__(self):
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"RasterImage must have positive dimensions, got {self.width}x{self.height}")
        bytes_per_pixel = len(self.mode)
        expected = self.width * self.height * bytes_per_pixel
        if len(self.data) != expected:
            raise ValueError(
                f"RasterImage data length {len(self.data)} does not match "
                f"{self.width}x{self.height}x{bytes_per_pixel} = {expected} for mode {self.mode!r}"
            )

    @property
    def stride(self) -> int:
        """Bytes per row (no padding)."""
        return self.width * len(self.mode)


# Extensions each loader claims. Mirrors _guess_file_type in core.project_db but
# is scoped to what Milestone 2 can actually render.
_PDF_EXTS = {".pdf"}
_DXF_EXTS = {".dxf"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def open_raster(path, **kwargs) -> RasterImage:
    """Decode *path* to a RasterImage, dispatching by extension.

    A thin convenience for callers (e.g. the UI) that just want "load whatever
    this file is". PDF-specific options (``page``, ``dpi``) pass through as
    keyword arguments. Loaders are imported lazily so importing this module
    costs nothing until a file of that type is actually opened.
    """
    from pathlib import Path

    ext = Path(path).suffix.lower()
    if ext in _PDF_EXTS:
        from .pdf_loader import load_pdf_page
        return load_pdf_page(path, **kwargs)
    if ext in _DXF_EXTS:
        from .dxf_loader import load_dxf_page
        return load_dxf_page(path, **kwargs)
    if ext in _IMAGE_EXTS:
        from .image_loader import load_image
        return load_image(path)
    raise ValueError(f"Unsupported file type for rendering: {ext!r}")
