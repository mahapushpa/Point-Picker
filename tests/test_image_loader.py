"""Tests for src.io.image_loader — runs with no Qt application.

Standard-library unittest; needs Pillow (a runtime dependency from Milestone 2).
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PIL import Image  # noqa: E402

from src.io.image_loader import load_image  # noqa: E402
from src.io.raster import RasterImage, open_raster  # noqa: E402


class ImageLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lmt_img_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, name, size=(8, 5), color=(10, 20, 30), mode="RGB"):
        path = self.tmp / name
        Image.new(mode, size, color).save(path)
        return path

    def test_load_png_returns_rgba_raster(self):
        path = self._make("a.png", size=(8, 5))
        r = load_image(path)
        self.assertIsInstance(r, RasterImage)
        self.assertEqual((r.width, r.height), (8, 5))
        self.assertEqual(r.mode, "RGBA")
        self.assertEqual(len(r.data), 8 * 5 * 4)
        self.assertEqual(r.stride, 8 * 4)

    def test_pixel_values_preserved(self):
        path = self._make("solid.png", size=(2, 2), color=(10, 20, 30))
        r = load_image(path)
        # First pixel should be (R,G,B,A) = (10,20,30,255)
        self.assertEqual(tuple(r.data[:4]), (10, 20, 30, 255))

    def test_jpeg_loads(self):
        path = self._make("b.jpg", size=(6, 4))
        r = load_image(path)
        self.assertEqual((r.width, r.height), (6, 4))
        self.assertEqual(r.mode, "RGBA")

    def test_grayscale_and_palette_convert_to_rgba(self):
        for mode, name in (("L", "g.png"), ("P", "p.png")):
            path = self._make(name, size=(4, 3), color=0 if mode == "L" else 1, mode=mode)
            r = load_image(path)
            self.assertEqual(r.mode, "RGBA")
            self.assertEqual(len(r.data), 4 * 3 * 4)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_image(self.tmp / "nope.png")

    def test_open_raster_dispatches_to_image_loader(self):
        path = self._make("dispatch.png", size=(3, 3))
        r = open_raster(path)
        self.assertEqual((r.width, r.height), (3, 3))

    def test_open_raster_rejects_unknown_extension(self):
        with self.assertRaises(ValueError):
            open_raster(self.tmp / "x.dxf")


if __name__ == "__main__":
    unittest.main()
