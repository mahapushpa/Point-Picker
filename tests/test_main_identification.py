"""Dialog/window-level tests for M10 identification templates + fields.

Drives the real IdentificationDialog and TemplatesDialog offscreen to confirm
the UI marshals correctly: applying a template populates the form, saving
persists fields/owner/notes, and editing a parcel never mutates the template.
Skipped if PySide6 is unavailable.
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
    from src.ui.identification_dialog import IdentificationDialog
    from src.ui.templates_dialog import TemplatesDialog
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
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class IdentificationDialogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = ProjectDB.create(str(Path(self._tmp.name) / "proj"))
        sample = Path(self._tmp.name) / "sheet.png"
        sample.write_bytes(b"x")
        self.sid = self.p.import_source(sample, "image")
        self.pid = self.p.create_parcel(self.sid, owner="Ramesh")
        agri_id = next(t["id"] for t in self.p.list_templates()
                       if t["name"] == "Rural — agricultural")
        self.agri = self.p.get_template(agri_id)   # includes ["fields"]

    def tearDown(self):
        self.p.close()
        self._tmp.cleanup()

    def _select_template(self, dlg, name):
        for i in range(dlg._template_combo.count()):
            if dlg._template_combo.itemText(i) == name:
                dlg._template_combo.setCurrentIndex(i)
                return
        raise AssertionError(f"template {name!r} not in combo")

    def test_dialog_loads_owner_and_source_reference(self):
        dlg = IdentificationDialog(self.p, self.pid)
        self.assertEqual(dlg._owner_edit.text(), "Ramesh")
        # Source reference line names the imported file.
        labels = [dlg.layout().itemAt(0).widget().text()]
        self.assertIn("sheet.png", labels[0])

    def test_apply_template_populates_the_fields_table(self):
        dlg = IdentificationDialog(self.p, self.pid)
        self._select_template(dlg, "Rural — agricultural")
        dlg._on_apply_template()
        labels = [r[0] for r in dlg._rows()]
        self.assertEqual(labels, self.agri["fields"])

    def test_save_persists_fields_owner_notes(self):
        dlg = IdentificationDialog(self.p, self.pid)
        self._select_template(dlg, "Rural — agricultural")
        dlg._on_apply_template()
        # Fill a value in the first row, change owner, add notes.
        dlg._table.item(0, 1).setText("K-123")
        dlg._owner_edit.setText("Ramesh Kumar")
        dlg._notes_edit.setPlainText("near the tubewell")
        dlg._on_save()

        parcel = self.p.get_parcel(self.pid)
        self.assertEqual(parcel["owner"], "Ramesh Kumar")
        self.assertEqual(parcel["notes"], "near the tubewell")
        fields = self.p.get_parcel_fields(self.pid)
        self.assertEqual(fields[0], {"label": "Khasra number", "value": "K-123"})
        self.assertEqual([f["label"] for f in fields], self.agri["fields"])

    def test_editing_via_dialog_does_not_mutate_template(self):
        before = self.p.get_template(self.agri["id"])["fields"]
        dlg = IdentificationDialog(self.p, self.pid)
        self._select_template(dlg, "Rural — agricultural")
        dlg._on_apply_template()
        # Rename a label and add a field in the parcel form, then save.
        dlg._table.item(0, 0).setText("Old Khasra")
        dlg._append_row("Mutation number", "M-9")
        dlg._on_save()
        after = self.p.get_template(self.agri["id"])["fields"]
        self.assertEqual(before, after)  # template unchanged by parcel editing

    def test_apply_in_dialog_is_additive_keeps_old_field(self):
        # Parcel already has an old Khasra; applying the residential template in
        # the dialog must keep it and add Plot number (additive), not replace.
        self.p.set_parcel_fields(self.pid, [("Khasra number", "K-42")])
        dlg = IdentificationDialog(self.p, self.pid)
        self._select_template(dlg, "Rural — residential")
        dlg._on_apply_template()
        rows = dict(dlg._rows())
        self.assertEqual(rows["Khasra number"], "K-42")  # survived in the table
        self.assertIn("Plot number", rows)               # added
        dlg._on_save()
        saved = {f["label"]: f["value"] for f in self.p.get_parcel_fields(self.pid)}
        self.assertEqual(saved["Khasra number"], "K-42")
        self.assertIn("Plot number", saved)

    def test_add_and_remove_field_rows(self):
        dlg = IdentificationDialog(self.p, self.pid)
        dlg._set_rows([])
        dlg._append_row("A", "1")
        dlg._append_row("B", "2")
        self.assertEqual(dlg._rows(), [("A", "1"), ("B", "2")])
        dlg._table.setCurrentCell(0, 0)
        dlg._remove_selected_row()
        self.assertEqual(dlg._rows(), [("B", "2")])


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class TemplatesDialogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = ProjectDB.create(str(Path(self._tmp.name) / "proj"))

    def tearDown(self):
        self.p.close()
        self._tmp.cleanup()

    def test_add_update_delete_user_template(self):
        dlg = TemplatesDialog(self.p)
        dlg._name_edit.setText("Orchard")
        dlg._labels_edit.setPlainText("Survey no\nVillage\nDistrict")
        dlg._on_add()
        tid = next(t["id"] for t in self.p.list_templates() if t["name"] == "Orchard")
        self.assertEqual(self.p.get_template(tid)["fields"], ["Survey no", "Village", "District"])

        # Update it via the dialog.
        for i in range(dlg._list.count()):
            if dlg._list.item(i).data(0x0100)["id"] == tid:
                dlg._list.setCurrentRow(i)
        dlg._labels_edit.setPlainText("Survey no\nTehsil")
        dlg._on_update()
        self.assertEqual(self.p.get_template(tid)["fields"], ["Survey no", "Tehsil"])

        # Delete it.
        dlg._on_delete()
        self.assertIsNone(self.p.get_template(tid))

    def test_builtin_selection_is_readonly(self):
        dlg = TemplatesDialog(self.p)
        for i in range(dlg._list.count()):
            if dlg._list.item(i).data(0x0100)["name"] == "Urban":
                dlg._list.setCurrentRow(i)
        self.assertFalse(dlg._update_btn.isEnabled())
        self.assertFalse(dlg._delete_btn.isEnabled())
        self.assertTrue(dlg._labels_edit.isReadOnly())


if __name__ == "__main__":
    unittest.main()
