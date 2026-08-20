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

from src.io.pdf_loader import (  # noqa: E402
    load_pdf_page, page_count, pdf_render_info, _render_dpi_for_page,
    DEFAULT_DPI, MAX_RENDER_PX,
)
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

    # -- B6: adaptive render DPI + precision flagging -----------------------

    def test_normal_page_renders_at_target_dpi(self):
        # An A4-ish page is well under the cap: rendered at exactly the target DPI,
        # not precision-limited, and byte-identical to the old fixed default.
        path = self._make_pdf(size=(595, 842))          # A4 in points
        dpi, limited = _render_dpi_for_page(595, 842)
        self.assertEqual(dpi, float(DEFAULT_DPI))
        self.assertFalse(limited)
        r = load_pdf_page(path)                          # default (adaptive) dpi
        # 842 pt = 11.69 in * 150 dpi ≈ 1754 px on the long side.
        self.assertAlmostEqual(max(r.width, r.height), 1754, delta=3)

    def test_huge_page_is_capped_and_flagged_precision_limited(self):
        # A page far larger than A0 on the long side: the target would exceed the
        # pixel ceiling, so DPI is reduced and precision_limited is reported.
        big_pt = 200 * 72                                 # 200 in — near the PDF max
        dpi, limited = _render_dpi_for_page(big_pt, big_pt)
        self.assertTrue(limited)
        self.assertLess(dpi, DEFAULT_DPI)
        # The reduced DPI keeps the longest side at (or just under) the ceiling.
        longest_px = (big_pt / 72.0) * dpi
        self.assertLessEqual(round(longest_px), MAX_RENDER_PX + 1)

    def test_pdf_render_info_reports_page_and_precision(self):
        path = self._make_pdf(size=(595, 842))
        info = pdf_render_info(path)
        self.assertEqual(info.target_dpi, DEFAULT_DPI)
        self.assertFalse(info.precision_limited)
        self.assertAlmostEqual(info.page_width_pt, 595, delta=1)


if __name__ == "__main__":
    unittest.main()
