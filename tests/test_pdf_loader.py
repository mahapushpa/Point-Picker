"""Tests for src.io.pdf_loader — runs with no Qt application.

Standard-library unittest; needs PyMuPDF (a runtime dependency from Milestone 2).
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

from src.io.pdf_loader import load_pdf_page, page_count  # noqa: E402
from src.io.raster import RasterImage, open_raster  # noqa: E402


class PdfLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lmt_pdf_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_pdf(self, name="doc.pdf", pages=1, size=(200, 100)):
        path = self.tmp / name
        doc = fitz.open()
        try:
            for _ in range(pages):
                doc.new_page(width=size[0], height=size[1])
            doc.save(str(path))
        finally:
            doc.close()
        return path

    def test_page_count(self):
        path = self._make_pdf(pages=3)
        self.assertEqual(page_count(path), 3)

    def test_render_returns_rgba_raster(self):
        path = self._make_pdf(size=(200, 100))
        r = load_pdf_page(path, page=0, dpi=72)
        self.assertIsInstance(r, RasterImage)
        self.assertGreater(r.width, 0)
        self.assertGreater(r.height, 0)
        self.assertEqual(r.mode, "RGBA")
        self.assertEqual(len(r.data), r.width * r.height * 4)
        # A 200x100pt page at 72 dpi renders ~200x100 px.
        self.assertAlmostEqual(r.width, 200, delta=2)
        self.assertAlmostEqual(r.height, 100, delta=2)

    def test_higher_dpi_yields_more_pixels(self):
        path = self._make_pdf(size=(100, 100))
        low = load_pdf_page(path, dpi=72)
        high = load_pdf_page(path, dpi=144)
        self.assertGreater(high.width, low.width)

    def test_out_of_range_page_raises(self):
        path = self._make_pdf(pages=1)
        with self.assertRaises(ValueError):
            load_pdf_page(path, page=5)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_pdf_page(self.tmp / "nope.pdf")

    def test_open_raster_dispatches_to_pdf_loader(self):
        path = self._make_pdf()
        r = open_raster(path, dpi=72)
        self.assertEqual(r.mode, "RGBA")


if __name__ == "__main__":
    unittest.main()
