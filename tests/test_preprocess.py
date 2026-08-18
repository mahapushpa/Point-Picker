"""Tests for src.io.preprocess — display-time denoise / contrast for scans (M8).

Pure io/: no Qt. Verifies the value-only invariant (dimensions never change),
that contrast enhancement measurably improves a low-contrast image, and that
denoise measurably reduces speckle noise.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    import numpy as np
    from src.io.raster import RasterImage
    from src.io.preprocess import preprocess_raster
    _HAVE_NUMPY = True
except Exception:  # pragma: no cover - environment without numpy/Pillow
    _HAVE_NUMPY = False


def _raster_from_gray(gray_u8):
    """Build an opaque RGBA RasterImage from a HxW uint8 luminance array."""
    h, w = gray_u8.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = gray_u8
    rgba[..., 3] = 255
    return RasterImage(w, h, rgba.tobytes(), "RGBA")


def _luma(raster):
    arr = np.frombuffer(raster.data, dtype=np.uint8).reshape(raster.height, raster.width, 4)
    rgb = arr[..., :3].astype(np.float64)
    return 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]


@unittest.skipUnless(_HAVE_NUMPY, "numpy/Pillow not available")
class PreprocessTests(unittest.TestCase):
    def _low_contrast_image(self, h=128, w=128):
        # Structured detail compressed into a narrow, dim band [116, 140].
        yy, xx = np.mgrid[0:h, 0:w]
        pattern = (np.sin(xx / 7.0) + np.cos(yy / 9.0)) * 0.5  # -1..1
        g = 128 + pattern * 12.0                               # ~116..140
        return _raster_from_gray(np.clip(g, 0, 255).astype(np.uint8))

    def _noisy_flat_image(self, h=128, w=128, seed=0):
        rng = np.random.default_rng(seed)
        g = np.full((h, w), 128, dtype=np.uint8)
        # ~8% salt-and-pepper speckle.
        mask = rng.random((h, w))
        g[mask < 0.04] = 0
        g[mask > 0.96] = 255
        return _raster_from_gray(g), np.full((h, w), 128, dtype=np.uint8)

    # -- the critical value-only invariant ----------------------------------

    def test_preserves_dimensions_and_mode(self):
        src = self._low_contrast_image(100, 137)  # deliberately non-square, odd
        out = preprocess_raster(src)
        self.assertEqual((out.width, out.height), (src.width, src.height))
        self.assertEqual(out.mode, "RGBA")
        self.assertEqual(len(out.data), src.width * src.height * 4)

    def test_input_not_mutated(self):
        src = self._low_contrast_image()
        before = bytes(src.data)
        preprocess_raster(src)
        self.assertEqual(src.data, before)  # raw raster untouched (non-destructive)

    def test_alpha_preserved(self):
        src = self._low_contrast_image(64, 64)
        out = preprocess_raster(src)
        a = np.frombuffer(out.data, dtype=np.uint8).reshape(64, 64, 4)[..., 3]
        self.assertTrue((a == 255).all())

    def test_various_sizes_keep_dimensions(self):
        for h, w in [(1, 1), (3, 3), (5, 200), (200, 5), (17, 33)]:
            src = self._low_contrast_image(h, w)
            out = preprocess_raster(src)
            self.assertEqual((out.width, out.height), (w, h), f"{w}x{h}")

    # -- measurable improvement ---------------------------------------------

    def test_contrast_enhancement_raises_contrast(self):
        src = self._low_contrast_image()
        before = float(_luma(src).std())
        out = preprocess_raster(src, denoise=False, contrast=True)
        after = float(_luma(out).std())
        self.assertGreater(after, before * 1.5,
                           f"contrast (std) should rise markedly: {before:.2f} -> {after:.2f}")

    def test_denoise_reduces_speckle(self):
        src, clean = self._noisy_flat_image()
        before_var = float(((_luma(src) - clean) ** 2).mean())
        out = preprocess_raster(src, denoise=True, contrast=False)
        after_var = float(((_luma(out) - clean) ** 2).mean())
        self.assertLess(after_var, before_var,
                        f"median denoise should cut speckle variance: {before_var:.1f} -> {after_var:.1f}")


if __name__ == "__main__":
    unittest.main()
