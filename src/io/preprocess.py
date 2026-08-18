"""preprocess — display-time denoise / contrast enhancement for scans (M8).

Pure Python (Pillow + numpy), **no UI-framework imports** — it operates on the
same neutral :class:`RasterImage` the loaders produce, so it sits cleanly inside
the io/ side of the io↔ui boundary.

Two standard, mechanical operations with sensible defaults (no tuning required):

  * **denoise** — a mild median filter, which removes speckle/salt-and-pepper
    from scans while preserving edges better than a blur;
  * **contrast** — CLAHE (Contrast-Limited Adaptive Histogram Equalisation) on the
    luminance channel, which lifts faint local detail on degraded scans without
    blowing out already-good regions the way a global stretch would.

CRITICAL invariant: preprocessing adjusts pixel **values only** — it never
resizes, crops, or rotates. Scale calibration (M3) and traced vertices (M6) are
stored in pixel coordinates of the loaded raster, so any change of dimensions
would silently invalidate every scale and vertex on that source. The output is a
brand-new RasterImage (the input is never mutated), and the dimensions are
asserted to match the input before it is returned.
"""

from __future__ import annotations

from .raster import RasterImage

# Defaults chosen to be a safe, generally-useful improvement on typical revenue
# scans without user tuning.
DEFAULT_MEDIAN_SIZE = 3      # 3x3 median: removes speckle, keeps thin ink lines
DEFAULT_CLAHE_TILES = 8      # 8x8 grid of local histogram regions
DEFAULT_CLAHE_CLIP = 2.0     # clip limit (x mean bin height) — limits noise amplification


def preprocess_raster(raster: RasterImage, *, denoise: bool = True,
                      contrast: bool = True, median_size: int = DEFAULT_MEDIAN_SIZE,
                      clahe_tiles: int = DEFAULT_CLAHE_TILES,
                      clahe_clip: float = DEFAULT_CLAHE_CLIP) -> RasterImage:
    """Return a denoised / contrast-enhanced copy of *raster*.

    Value-only: the returned RasterImage has the exact same width and height and
    the input is never modified. Alpha is carried through untouched.
    """
    from PIL import Image, ImageFilter
    import numpy as np

    if raster.mode != "RGBA":
        raise ValueError(f"preprocess expects RGBA rasters, got mode {raster.mode!r}")

    img = Image.frombytes("RGBA", (raster.width, raster.height), raster.data)
    r, g, b, a = img.split()
    rgb = Image.merge("RGB", (r, g, b))

    if denoise:
        rgb = rgb.filter(ImageFilter.MedianFilter(size=median_size))

    if contrast:
        # Equalise on luminance so colour (chroma) is preserved: near-grayscale
        # scans still read naturally, just clearer.
        y, cb, cr = rgb.convert("YCbCr").split()
        y_arr = np.asarray(y, dtype=np.uint8)
        y_eq = _clahe(y_arr, np, tiles=clahe_tiles, clip_limit=clahe_clip)
        y_new = Image.fromarray(y_eq, mode="L")
        rgb = Image.merge("YCbCr", (y_new, cb, cr)).convert("RGB")

    r2, g2, b2 = rgb.split()
    out = Image.merge("RGBA", (r2, g2, b2, a))

    # Enforce the value-only invariant explicitly, rather than trusting it.
    assert out.size == (raster.width, raster.height), (
        f"preprocessing changed dimensions {out.size} != "
        f"{(raster.width, raster.height)} — would invalidate scale/vertices")

    result = RasterImage(raster.width, raster.height, out.tobytes(), "RGBA")
    assert result.width == raster.width and result.height == raster.height
    return result


def _tile_lut(block, np, clip_limit: float):
    """256-entry intensity→intensity mapping (0..255) for one tile, from its
    clip-limited, cumulative-histogram equalisation."""
    if block.size == 0:
        return np.arange(256, dtype=np.float64)
    hist = np.bincount(block.ravel(), minlength=256).astype(np.float64)
    if clip_limit > 0:
        # Clip tall bins and spread the clipped mass uniformly — this is the
        # "contrast-limited" part: it caps noise amplification in flat regions.
        limit = max(1.0, clip_limit * block.size / 256.0)
        excess = np.maximum(hist - limit, 0.0).sum()
        hist = np.minimum(hist, limit) + excess / 256.0
    cdf = np.cumsum(hist)
    total = cdf[-1]
    if total <= 0:
        return np.arange(256, dtype=np.float64)
    return cdf / total * 255.0


def _axis_interp(n: int, centers, np):
    """For a length-*n* axis, return (i0, i1, w): for each coordinate, the two
    bracketing tile indices and the weight toward i1, for bilinear blending of
    neighbouring tile mappings (this is what makes CLAHE seamless across tiles)."""
    coords = np.arange(n)
    idx = np.searchsorted(centers, coords)
    i1 = np.clip(idx, 0, len(centers) - 1)
    i0 = np.clip(idx - 1, 0, len(centers) - 1)
    c0 = centers[i0]
    c1 = centers[i1]
    denom = np.where(c1 > c0, c1 - c0, 1.0)
    w = np.where(c1 > c0, (coords - c0) / denom, 0.0)
    return i0, i1, np.clip(w, 0.0, 1.0)


def _clahe(gray, np, *, tiles: int, clip_limit: float):
    """Contrast-Limited Adaptive Histogram Equalisation on a uint8 HxW array.

    Builds a clip-limited equalisation mapping per tile, then blends the four
    surrounding tile mappings bilinearly at every pixel so there are no tile
    seams. Returns a uint8 array the same shape as *gray*."""
    h, w = gray.shape
    ty = max(1, min(tiles, h))
    tx = max(1, min(tiles, w))

    ys = [int(round(k * h / ty)) for k in range(ty + 1)]
    xs = [int(round(k * w / tx)) for k in range(tx + 1)]

    luts = np.empty((ty, tx, 256), dtype=np.float64)
    centers_y = np.empty(ty, dtype=np.float64)
    centers_x = np.empty(tx, dtype=np.float64)
    for i in range(ty):
        centers_y[i] = (ys[i] + ys[i + 1] - 1) / 2.0
        for j in range(tx):
            luts[i, j] = _tile_lut(gray[ys[i]:ys[i + 1], xs[j]:xs[j + 1]], np, clip_limit)
    for j in range(tx):
        centers_x[j] = (xs[j] + xs[j + 1] - 1) / 2.0

    iy0, iy1, wy = _axis_interp(h, centers_y, np)
    ix0, ix1, wx = _axis_interp(w, centers_x, np)

    # result[y, x] = bilinear blend over the 4 surrounding tile LUTs, each
    # evaluated at the pixel's original intensity gray[y, x].
    v = gray
    c00 = luts[iy0[:, None], ix0[None, :], v]
    c01 = luts[iy0[:, None], ix1[None, :], v]
    c10 = luts[iy1[:, None], ix0[None, :], v]
    c11 = luts[iy1[:, None], ix1[None, :], v]
    wy = wy[:, None]
    wx = wx[None, :]
    out = ((1 - wy) * (1 - wx) * c00 + (1 - wy) * wx * c01 +
           wy * (1 - wx) * c10 + wy * wx * c11)
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)
