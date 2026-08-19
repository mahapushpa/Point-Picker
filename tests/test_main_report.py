"""Window-level test for M11 owner-wise report export.

Drives MainWindow.export_owner_report offscreen with the preview dialog stubbed,
confirming the menu action: scopes by M7's selection, previews coverage, and
writes never-overwriting files into the project's exports/ folder. All report
logic itself is covered by test_report.py. Skipped without PySide6.
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
    from src.core.project_db import ProjectDB
    from src.ui import main_window as MW
    from src.ui.main_window import MainWindow
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)


class _FakeDialog:
    """Stand-in for OwnerReportDialog: records what it was shown, auto-accepts,
    and returns preset formats. Class attributes let a test tweak behaviour."""
    accept = True
    formats = ["csv"]
    last_scope = None
    last_report = None

    def __init__(self, report, scope_label, parent=None):
        type(self).last_scope = scope_label
        type(self).last_report = report

    def exec(self):
        return 1 if type(self).accept else 0

    def selected_formats(self):
        return list(type(self).formats)


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class OwnerReportExportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = ProjectDB.create(str(Path(self._tmp.name) / "proj"), name="Demo")
        sheet = Path(self._tmp.name) / "sheet.png"
        sheet.write_bytes(b"x")
        self.sid = self.proj.import_source(sheet, "image")
        self.proj.set_source_scale(self.sid, 0.5, method="two-point")
        self.pid1 = self.proj.create_parcel(self.sid, owner="Ramesh")
        self.proj.save_parcel_polygon(
            self.pid1, [(0, 0), (100, 0), (100, 100), (0, 100)], closed=True,
            metres_per_pixel=0.5)
        self.pid2 = self.proj.create_parcel(self.sid, owner="Sita")
        self.proj.save_parcel_polygon(
            self.pid2, [(0, 0), (50, 0), (50, 50), (0, 50)], closed=True,
            metres_per_pixel=0.5)

        # Patch the preview dialog with a headless stub.
        self._real_dialog = MW.ReportDialog
        MW.ReportDialog = _FakeDialog
        _FakeDialog.accept = True
        _FakeDialog.formats = ["csv"]
        _FakeDialog.last_scope = None

        self.win = MainWindow()
        self.win._set_project(self.proj)
        # Reflect a loaded sheet so selected_parcel_ids() (which is scoped to the
        # current source's parcels) has something to filter, as it would in use.
        self.win._source_id = self.sid
        self.win._parcels = self.proj.list_parcels(self.sid)

    def tearDown(self):
        MW.ReportDialog = self._real_dialog
        self.win.close()
        try:
            self.proj.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _exports(self, pattern):
        return sorted((self.proj.exports_dir).glob(pattern))

    def test_export_writes_one_csv_per_owner_into_exports_folder(self):
        self.win.export_owner_report()
        files = self._exports("owner-summary_*.csv")
        self.assertEqual(len(files), 2)                       # Ramesh + Sita
        self.assertTrue(list(self.proj.exports_dir.glob("*_ramesh_*.csv")))
        self.assertTrue(list(self.proj.exports_dir.glob("*_sita_*.csv")))
        # Default scope is the whole project (no selection active).
        self.assertIn("all parcels", _FakeDialog.last_scope)

    def test_each_owners_formats_share_a_stem(self):
        _FakeDialog.formats = ["pdf", "csv", "json"]
        self.win.export_owner_report()
        # 2 owners × 3 formats = 6 files; the three formats of an owner share a
        # stem, so distinct stems == owner count.
        stems = {p.stem for p in self.proj.exports_dir.glob("owner-summary_*")}
        self.assertEqual(len(stems), 2)
        for ext in ("pdf", "csv", "json"):
            self.assertEqual(len(list(self.proj.exports_dir.glob(f"owner-summary_*.{ext}"))), 2)

    def test_regenerating_never_overwrites(self):
        self.win.export_owner_report()
        self.win.export_owner_report()
        # Two generations -> Ramesh has two distinct CSV files, nothing clobbered.
        self.assertEqual(len(list(self.proj.exports_dir.glob("*_ramesh_*.csv"))), 2)

    def test_selection_scopes_the_report(self):
        # Select only Ramesh's parcel; the report should cover just that one owner.
        self.win.set_parcel_selected(self.pid1, True)
        _FakeDialog.formats = ["json"]
        self.win.export_owner_report()
        self.assertIn("1 selected parcel", _FakeDialog.last_scope)
        self.assertEqual(_FakeDialog.last_report.parcel_count, 1)
        self.assertEqual(_FakeDialog.last_report.groups[0].owner, "Ramesh")
        # Only Ramesh's file is produced.
        self.assertTrue(list(self.proj.exports_dir.glob("*_ramesh_*.json")))
        self.assertEqual(list(self.proj.exports_dir.glob("*_sita_*.json")), [])

    def test_cancel_writes_nothing(self):
        _FakeDialog.accept = False
        self.win.export_owner_report()
        self.assertEqual(self._exports("owner-summary_*"), [])

    def test_no_project_is_a_no_op(self):
        self.win._project = None
        # Must not raise or write anything.
        self.win.export_owner_report()


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class ReportDialogFormatTests(unittest.TestCase):
    """The real preview dialog: PDF is always written, CSV/JSON are opt-in."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = ProjectDB.create(str(Path(self._tmp.name) / "proj"), name="Demo")
        sheet = Path(self._tmp.name) / "sheet.png"
        sheet.write_bytes(b"x")
        sid = self.proj.import_source(sheet, "image")
        self.proj.set_source_scale(sid, 0.5, method="two-point")
        pid = self.proj.create_parcel(sid, owner="Ramesh")
        self.proj.save_parcel_polygon(
            pid, [(0, 0), (100, 0), (100, 100), (0, 100)], closed=True,
            metres_per_pixel=0.5)
        self.report = MW.report_export.build_owner_report(self.proj)

    def tearDown(self):
        self.proj.close()
        self._tmp.cleanup()

    def test_pdf_always_selected_by_default(self):
        from src.ui.report_dialog import OwnerReportDialog
        dlg = OwnerReportDialog(self.report, "all parcels in the project")
        self.assertEqual(dlg.selected_formats(), ["pdf"])

    def test_csv_and_json_are_opt_in_extras(self):
        from src.ui.report_dialog import OwnerReportDialog
        dlg = OwnerReportDialog(self.report, "all parcels in the project")
        dlg._csv.setChecked(True)
        dlg._json.setChecked(True)
        self.assertEqual(dlg.selected_formats(), ["pdf", "csv", "json"])


if __name__ == "__main__":
    unittest.main()
