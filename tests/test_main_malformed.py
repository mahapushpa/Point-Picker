"""Window-level tests for B5 — malformed source input must fail gracefully.

The normal source-open path (MainWindow.load_path) must never crash or hang on a
bad file: a non-image renamed to .png/.jpg, an empty file, or a truncated/corrupt
PDF must each surface a clear error dialog and leave the window usable (a good
file still opens afterwards). Skipped without PySide6.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from src.ui.main_window import MainWindow
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False

try:
    from PIL import Image
    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False

try:
    import pymupdf as _fitz
    _HAVE_PYMUPDF = True
except Exception:  # pragma: no cover
    try:
        import fitz as _fitz
        _HAVE_PYMUPDF = True
    except Exception:
        _HAVE_PYMUPDF = False

_app = None
_criticals: list = []


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)
        # Record every critical (error) dialog so tests can assert a graceful
        # message was shown instead of a crash.
        QMessageBox.critical = staticmethod(
            lambda *a, **k: _criticals.append(a[2] if len(a) > 2 else ""))


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class MalformedInputTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.win = MainWindow()
        _criticals.clear()

    def tearDown(self):
        self.win.close()
        self._tmp.cleanup()

    def _assert_graceful(self, path):
        """load_path must return without raising, and have shown a critical error."""
        before = len(_criticals)
        try:
            self.win.load_path(str(path))   # must not raise
        except Exception as exc:  # noqa: BLE001
            self.fail(f"load_path raised instead of showing an error dialog: {exc!r}")
        self.assertGreater(len(_criticals), before,
                           "no error dialog was shown for malformed input")

    @unittest.skipUnless(_HAVE_PIL, "Pillow not available")
    def test_non_image_renamed_to_png(self):
        p = self.root / "fake.png"
        p.write_bytes(b"This is plain text, not a PNG, despite the extension.\n" * 5)
        self._assert_graceful(p)

    @unittest.skipUnless(_HAVE_PIL, "Pillow not available")
    def test_non_image_renamed_to_jpg(self):
        p = self.root / "fake.jpg"
        p.write_bytes(b"\x00\x01\x02 not really a jpeg \xff\xd8 partial marker only")
        self._assert_graceful(p)

    def test_empty_png_file(self):
        p = self.root / "empty.png"
        p.write_bytes(b"")
        self._assert_graceful(p)

    def test_empty_pdf_file(self):
        p = self.root / "empty.pdf"
        p.write_bytes(b"")
        self._assert_graceful(p)

    def test_garbage_pdf_bytes(self):
        # A file with a .pdf extension that is not a PDF at all.
        p = self.root / "garbage.pdf"
        p.write_bytes(b"this is not a pdf, not even close" * 8)
        self._assert_graceful(p)

    @unittest.skipUnless(_HAVE_PYMUPDF, "PyMuPDF not available")
    def test_severely_truncated_pdf(self):
        # Keep only the first 40 bytes of a valid PDF — well below a viable
        # document, so MuPDF cannot repair it and fails cleanly (contrast with a
        # *mildly* truncated PDF, which MuPDF auto-repairs and renders best-effort;
        # that path does not error because MuPDF reports success — see the review
        # notes. This asserts the genuinely-unopenable case fails gracefully).
        good = self.root / "good.pdf"
        doc = _fitz.open()
        doc.new_page(width=216, height=144)
        doc.save(str(good))
        doc.close()
        p = self.root / "truncated.pdf"
        p.write_bytes(good.read_bytes()[:40])
        self._assert_graceful(p)

    @unittest.skipUnless(_HAVE_PIL, "Pillow not available")
    def test_window_still_usable_after_a_bad_file(self):
        # A bad open must not wedge the window: a good image still loads after.
        bad = self.root / "bad.png"
        bad.write_bytes(b"nope")
        self._assert_graceful(bad)
        good = self.root / "ok.png"
        Image.new("RGB", (80, 60), (255, 255, 255)).save(good)
        self.win.load_path(str(good))
        self.assertIsNotNone(self.win._raw_raster)
        self.assertEqual((self.win._raw_raster.width, self.win._raw_raster.height), (80, 60))


if __name__ == "__main__":
    unittest.main()
