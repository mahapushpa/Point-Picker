"""image_loader — load a raster image (PNG/JPEG/BMP/TIFF) to a RasterImage.

Pure Python, no UI-framework imports. Uses Pillow to decode and normalise to
8-bit RGBA, returning a neutral :class:`~src.io.raster.RasterImage` so the Qt
layer can build a QImage without io/ ever depending on Qt.

Reading DPI metadata as a scale clue is deferred to the scale milestones; here
we only decode pixels for display (Milestone 2).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from .raster import RasterImage


def load_image(path) -> RasterImage:
    """Decode an image file to an RGBA :class:`RasterImage`.

    Raises ``FileNotFoundError`` if missing, or Pillow's ``UnidentifiedImageError``
    if the file isn't a decodable image.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Image not found: {p}")

    with Image.open(p) as img:
        # Normalise every input (palette, grayscale, CMYK, RGB, RGBA...) to RGBA
        # so downstream code has one predictable pixel layout.
        rgba = img.convert("RGBA")
        return RasterImage(
            width=rgba.width,
            height=rgba.height,
            data=rgba.tobytes(),
            mode="RGBA",
        )
