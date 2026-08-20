"""Window/dialog tests for C8 — optional reference-document panel.

The identification dialog can attach a digitally-generated PDF to the source and
show its text for copy-paste — but it NEVER auto-fills a field, and a scan (no
text layer) or no attachment leaves the form exactly as before. Skipped without
PySide6 / PyMuPDF.
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
    from PySide6.QtWidgets import QApplication
    from src.core.project_db import ProjectDB
    from src.ui.identification_dialog import IdentificationDialog
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False

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


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


def _text_pdf(path, text):
    doc = _fitz.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((40, 60), text)
    doc.save(str(path))
    doc.close()


def _scan_pdf(path):
    doc = _fitz.open()
    doc.new_page(width=300, height=200)   # no text inserted
    doc.save(str(path))
    doc.close()


@unittest.skipUnless(_HAVE_QT and _HAVE_PYMUPDF, "PySide6 / PyMuPDF not available")
class ReferenceDocPanelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.proj = ProjectDB.create(str(self.root / "proj"), name="Demo")
        sheet = self.root / "sheet.png"
        sheet.write_bytes(b"img")
        self.sid = self.proj.import_source(sheet, "image")
        self.pid = self.proj.create_parcel(self.sid, owner="Ramesh")

    def tearDown(self):
        self.proj.close()
        self._tmp.cleanup()

    def _dialog(self):
        return IdentificationDialog(self.proj, self.pid)

    def test_no_attachment_shows_placeholder_and_empty_fields(self):
        dlg = self._dialog()
        self.assertEqual(dlg._ref_text.toPlainText(), "")
        self.assertFalse(dlg._ref_detach_btn.isEnabled())
        # The form is untouched: no fields invented.
        self.assertEqual(dlg._rows(), [])

    def test_attached_text_pdf_shows_extracted_text_but_does_not_autofill(self):
        ref = self.root / "extract.pdf"
        _text_pdf(ref, "Khasra 123 Khata 45 Owner Ramesh")
        self.proj.attach_reference_doc(self.sid, ref)
        dlg = self._dialog()
        self.assertIn("Khasra 123", dlg._ref_text.toPlainText())
        # Critically: showing the text must NOT populate any identification field.
        self.assertEqual(dlg._rows(), [])
        self.assertEqual(self.proj.get_parcel_fields(self.pid), [])

    def test_attached_scan_pdf_shows_no_text_message(self):
        ref = self.root / "scan.pdf"
        _scan_pdf(ref)
        self.proj.attach_reference_doc(self.sid, ref)
        dlg = self._dialog()
        self.assertIn("No text layer", dlg._ref_text.toPlainText())

    def test_reference_text_is_read_only(self):
        ref = self.root / "extract.pdf"
        _text_pdf(ref, "some text")
        self.proj.attach_reference_doc(self.sid, ref)
        dlg = self._dialog()
        self.assertTrue(dlg._ref_text.isReadOnly())

    def test_detach_clears_panel(self):
        ref = self.root / "extract.pdf"
        _text_pdf(ref, "text here")
        self.proj.attach_reference_doc(self.sid, ref)
        dlg = self._dialog()
        self.assertTrue(dlg._ref_detach_btn.isEnabled())
        dlg._on_detach_reference()
        self.assertEqual(dlg._ref_text.toPlainText(), "")
        self.assertIsNone(self.proj.get_reference_doc(self.sid))


if __name__ == "__main__":
    unittest.main()
