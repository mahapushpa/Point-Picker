"""Tests for Milestone 14: PDF metadata-based scale detection (method 1) and the
cross-check between two scales (method 4).

The arithmetic + plausibility checks are pure (src.core.scale) and always run;
the end-to-end read from a real generated PDF needs PyMuPDF and is skipped when
it isn't installed.
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.scale import (
    MetadataScale, compare_scales, metadata_scale_from_page, points_to_metres,
    METHOD_PDF_METADATA, MAX_PAGE_POINTS,
)

try:
    from src.io.pdf_loader import read_page_size_points, load_pdf_page
    import pymupdf as _fitz  # noqa: F401
    _HAVE_PYMUPDF = True
except Exception:  # pragma: no cover - depends on install
    try:
        from src.io.pdf_loader import read_page_size_points, load_pdf_page
        import fitz as _fitz  # noqa: F401
        _HAVE_PYMUPDF = True
    except Exception:
        _HAVE_PYMUPDF = False


class MetadataScaleMathTests(unittest.TestCase):
    def test_points_to_metres(self):
        self.assertAlmostEqual(points_to_metres(72.0), 0.0254)      # 1 inch
        self.assertAlmostEqual(points_to_metres(0.0), 0.0)

    def test_known_page_gives_expected_mpp(self):
        # A square 7200 pt (100 inch = 2.54 m) page rendered to 1000x1000 px:
        # each pixel spans 2.54 / 1000 = 0.00254 m.
        ms = metadata_scale_from_page(7200.0, 7200.0, 1000, 1000)
        self.assertIsInstance(ms, MetadataScale)
        self.assertEqual(ms.method, METHOD_PDF_METADATA)
        self.assertAlmostEqual(ms.width_m, 2.54)
        self.assertAlmostEqual(ms.metres_per_pixel, 0.00254)
        self.assertAlmostEqual(ms.pixels_per_metre, 1.0 / 0.00254)

    def test_non_square_page_averages_consistent_axes(self):
        # 200x100 pt to a proportional raster: both axes imply the same mpp.
        ms = metadata_scale_from_page(200.0, 100.0, 400, 200)
        self.assertAlmostEqual(ms.metres_per_pixel, points_to_metres(200.0) / 400)

    def test_zero_or_negative_raster_is_rejected(self):
        with self.assertRaises(ValueError):
            metadata_scale_from_page(200.0, 100.0, 0, 200)
        with self.assertRaises(ValueError):
            metadata_scale_from_page(200.0, 100.0, 400, -1)

    def test_implausible_page_sizes_are_rejected(self):
        with self.assertRaises(ValueError):
            metadata_scale_from_page(0.0, 100.0, 400, 200)          # zero page
        with self.assertRaises(ValueError):
            metadata_scale_from_page(0.5, 0.5, 400, 200)            # absurdly small
        with self.assertRaises(ValueError):
            metadata_scale_from_page(MAX_PAGE_POINTS + 1, 100.0, 400, 200)  # absurdly large


class CrossCheckTests(unittest.TestCase):
    def test_close_scales_agree(self):
        cc = compare_scales(0.00254, 0.00256)
        self.assertLess(cc.percent_difference, 2.0)
        self.assertTrue(cc.agree)
        self.assertIn("agree", cc.describe())

    def test_divergent_scales_flagged(self):
        # Manual 0.04 vs metadata 0.00254 — wildly different (a scaled map vs the
        # 1:1 metadata assumption): must be reported as disagreement, not resolved.
        cc = compare_scales(0.04, 0.00254)
        self.assertFalse(cc.agree)
        self.assertGreater(cc.percent_difference, 50.0)
        self.assertIn("DISAGREE", cc.describe())

    def test_percent_is_symmetric(self):
        a = compare_scales(0.01, 0.011).percent_difference
        b = compare_scales(0.011, 0.01).percent_difference
        self.assertAlmostEqual(a, b)

    def test_tolerance_is_configurable(self):
        self.assertFalse(compare_scales(0.010, 0.0105).agree)              # ~4.9% > 2%
        self.assertTrue(compare_scales(0.010, 0.0105, tolerance_percent=6.0).agree)

    def test_non_positive_scales_rejected(self):
        with self.assertRaises(ValueError):
            compare_scales(0.0, 0.01)


@unittest.skipUnless(_HAVE_PYMUPDF, "PyMuPDF not available")
class PdfMetadataEndToEndTests(unittest.TestCase):
    def _make_pdf(self, width_pt, height_pt):
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "page.pdf"
        doc = _fitz.open()
        doc.new_page(width=width_pt, height=height_pt)
        doc.save(str(path))
        doc.close()
        return path

    def test_read_page_size_matches_what_was_authored(self):
        path = self._make_pdf(200.0, 100.0)
        w, h = read_page_size_points(path)
        self.assertAlmostEqual(w, 200.0, places=3)
        self.assertAlmostEqual(h, 100.0, places=3)

    def test_derived_scale_matches_render_dpi(self):
        # A page rendered at DPI has each pixel spanning 1/DPI inch = 0.0254/DPI m.
        path = self._make_pdf(216.0, 144.0)   # 3in x 2in
        dpi = 150
        raster = load_pdf_page(path, dpi=dpi)
        w_pt, h_pt = read_page_size_points(path)
        ms = metadata_scale_from_page(w_pt, h_pt, raster.width, raster.height)
        self.assertAlmostEqual(ms.metres_per_pixel, 0.0254 / dpi, places=6)

    def test_missing_pdf_raises(self):
        with self.assertRaises(FileNotFoundError):
            read_page_size_points(Path(tempfile.mkdtemp()) / "nope.pdf")


if __name__ == "__main__":
    unittest.main()
