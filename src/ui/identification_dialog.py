"""identification_dialog — a parcel's identification / revenue-record form (M10).

Gathers the always-present data (source-document reference, owner, free-text
notes) and the parcel's own ``{label, value}`` identification fields. A land-type
template can be *applied* to populate the field labels as a starting point; the
template is never modified by editing here (applying is a one-way copy in
``ProjectDB.apply_template_to_parcel``).

Owner is edited here too but is stored as the first-class ``parcels.owner`` column
(the report grouping key), not as a renamable field. Storage/validation live in
``project_db``; this dialog only marshals widgets.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..core.project_db import ProjectError


class IdentificationDialog(QDialog):
    """Edit one parcel's identification fields, owner, and notes."""

    def __init__(self, project, parcel_id: int, parent=None):
        super().__init__(parent)
        self._project = project
        self._parcel_id = parcel_id
        self.setWindowTitle("Parcel identification")
        self.resize(860, 560)   # wide: form on the left, reference panel on the right

        parcel = project.get_parcel(parcel_id) or {}
        self._source_id = parcel.get("source_id")

        outer = QVBoxLayout(self)
        body = QHBoxLayout()
        outer.addLayout(body, 1)

        # Left column: the identification form.
        layout = QVBoxLayout()
        body.addLayout(layout, 3)

        # Always present: the source-document reference (read-only).
        self._source_ref_label = QLabel(self._source_reference_text(parcel.get("source_id")))
        layout.addWidget(self._source_ref_label)

        # Template chooser (a starting point; does not lock the parcel to it).
        tmpl_row = QHBoxLayout()
        tmpl_row.addWidget(QLabel("Template:"))
        self._template_combo = QComboBox()
        self._templates = project.list_templates()
        for tmpl in self._templates:
            self._template_combo.addItem(tmpl["name"], tmpl["id"])
        tmpl_row.addWidget(self._template_combo, 1)
        self._apply_btn = QPushButton("Apply template")
        self._apply_btn.setToolTip("Populate the fields below from this template (values you've "
                                   "already entered are kept). The template is not changed.")
        self._apply_btn.clicked.connect(self._on_apply_template)
        tmpl_row.addWidget(self._apply_btn)
        layout.addLayout(tmpl_row)

        # Owner — always present, stored as the parcels.owner column.
        owner_row = QHBoxLayout()
        owner_row.addWidget(QLabel("Owner:"))
        self._owner_edit = QLineEdit(parcel.get("owner") or "")
        self._owner_edit.setPlaceholderText("owner / tenant / patta-holder")
        owner_row.addWidget(self._owner_edit, 1)
        layout.addLayout(owner_row)

        # Identification fields (label / value pairs).
        layout.addWidget(QLabel("Identification fields:"))
        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["Label", "Value"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self._table, 1)
        self._set_rows([(f["label"], f["value"]) for f in project.get_parcel_fields(parcel_id)])

        row_btns = QHBoxLayout()
        add_btn = QPushButton("Add field")
        add_btn.setToolTip("Add a blank label/value row (e.g. an extra identifier or address level)")
        add_btn.clicked.connect(lambda: self._append_row("", ""))
        remove_btn = QPushButton("Remove selected")
        remove_btn.setToolTip("Remove the selected field row from this parcel")
        remove_btn.clicked.connect(self._remove_selected_row)
        row_btns.addWidget(add_btn)
        row_btns.addWidget(remove_btn)
        row_btns.addStretch(1)
        layout.addLayout(row_btns)

        # Notes — always present, stored as the parcels.notes column.
        layout.addWidget(QLabel("Notes:"))
        self._notes_edit = QPlainTextEdit(parcel.get("notes") or "")
        self._notes_edit.setMaximumHeight(80)
        layout.addWidget(self._notes_edit)

        # Right column: the optional reference-document panel (C8).
        body.addWidget(self._build_reference_panel(), 2)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                               QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._on_save)
        box.rejected.connect(self.reject)
        outer.addWidget(box)

    # -- reference document panel (C8) --------------------------------------

    def _build_reference_panel(self) -> QFrame:
        """A read-only side panel showing the text of an optional reference
        document (e.g. a digitally-generated jamabandi extract) so exact values
        can be copied by hand into the fields on the left. Nothing here ever
        auto-fills a field or guesses meaning."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        v = QVBoxLayout(frame)

        v.addWidget(QLabel("<b>Reference document</b> (optional)"))
        hint = QLabel("Attach a digitally-generated PDF (e.g. a jamabandi extract) "
                      "to read its text here and copy exact values into the fields "
                      "on the left. It is never traced or measured, and nothing is "
                      "auto-filled.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        v.addWidget(hint)

        row = QHBoxLayout()
        self._ref_name_label = QLabel()
        self._ref_name_label.setWordWrap(True)
        row.addWidget(self._ref_name_label, 1)
        self._ref_attach_btn = QPushButton("Attach…")
        self._ref_attach_btn.setToolTip("Attach a reference PDF (copied into the project; never modified)")
        self._ref_attach_btn.clicked.connect(self._on_attach_reference)
        row.addWidget(self._ref_attach_btn)
        self._ref_detach_btn = QPushButton("Detach")
        self._ref_detach_btn.setToolTip("Remove the reference attachment (the copied file stays in the project)")
        self._ref_detach_btn.clicked.connect(self._on_detach_reference)
        row.addWidget(self._ref_detach_btn)
        v.addLayout(row)

        self._ref_text = QPlainTextEdit()
        self._ref_text.setReadOnly(True)          # copyable, never edited/auto-applied
        self._ref_text.setPlaceholderText("No reference document attached.")
        v.addWidget(self._ref_text, 1)

        self._reload_reference()
        return frame

    def _reload_reference(self) -> None:
        """Refresh the panel from the current attachment state."""
        if self._source_id is None or self._project is None:
            self._ref_name_label.setText("<i>Attach a source to a project first.</i>")
            self._ref_attach_btn.setEnabled(False)
            self._ref_detach_btn.setEnabled(False)
            self._ref_text.setPlainText("")
            return
        self._ref_attach_btn.setEnabled(True)
        ref = self._project.get_reference_doc(self._source_id)
        if ref is None:
            self._ref_name_label.setText("<i>None attached.</i>")
            self._ref_detach_btn.setEnabled(False)
            self._ref_text.setPlainText("")
            return
        self._ref_detach_btn.setEnabled(True)
        self._ref_name_label.setText(ref["original_name"] or ref["relative_path"])
        self._ref_text.setPlainText(self._extract_reference_text(ref["resolved_path"]))

    def _extract_reference_text(self, resolved_path) -> str:
        """The reference PDF's text, or a clear note when there's none (a scan) or
        it can't be read. Never raises into the UI."""
        p = Path(resolved_path)
        if not p.is_file():
            return "(The attached reference file is missing from the project.)"
        if p.suffix.lower() != ".pdf":
            return "(Reference text is only extracted from PDFs.)"
        try:
            from ..io.pdf_loader import extract_text
            text = extract_text(p)
        except Exception as exc:  # noqa: BLE001
            return f"(Could not read reference text: {exc})"
        if not text:
            return ("(No text layer — this PDF looks like a scan, so there is "
                    "nothing to copy. Enter the fields manually.)")
        return text

    def _on_attach_reference(self) -> None:
        if self._source_id is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Attach reference PDF", "", "PDF documents (*.pdf)")
        if not path:
            return
        try:
            self._project.attach_reference_doc(self._source_id, path)
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not attach", str(exc))
            return
        self._reload_reference()

    def _on_detach_reference(self) -> None:
        if self._source_id is None:
            return
        self._project.clear_reference_doc(self._source_id)
        self._reload_reference()

    # -- helpers ------------------------------------------------------------

    def _source_reference_text(self, source_id) -> str:
        if source_id is None or self._project is None:
            return "Source: (none)"
        src = self._project.get_source(source_id)
        if not src:
            return "Source: (none)"
        parts = [src.get("original_name") or src.get("relative_path") or "?"]
        if src.get("page") is not None:
            parts.append(f"page {src['page']}")
        if src.get("doc_date"):
            parts.append(str(src["doc_date"]))
        return "Source: " + ", ".join(parts)

    def _set_rows(self, rows) -> None:
        self._table.setRowCount(0)
        for label, value in rows:
            self._append_row(label, value)

    def _append_row(self, label: str, value: str) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)
        self._table.setItem(r, 0, QTableWidgetItem(label or ""))
        self._table.setItem(r, 1, QTableWidgetItem("" if value is None else str(value)))

    def _remove_selected_row(self) -> None:
        r = self._table.currentRow()
        if r >= 0:
            self._table.removeRow(r)

    def _rows(self) -> list[tuple[str, str]]:
        out = []
        for r in range(self._table.rowCount()):
            label_item = self._table.item(r, 0)
            value_item = self._table.item(r, 1)
            label = label_item.text().strip() if label_item else ""
            value = value_item.text() if value_item else ""
            out.append((label, value))
        return out

    # -- actions ------------------------------------------------------------

    def _on_apply_template(self) -> None:
        """Add the chosen template's labels to the fields table — **additive**:
        existing rows (and their values) are kept, and any template label not
        already present is appended (empty). Nothing is removed, so old
        identifiers survive when a different template is applied later. In-memory
        only until Save, and the template itself is never modified."""
        tid = self._template_combo.currentData()
        if tid is None:
            return
        template = self._project.get_template(tid)
        if template is None:
            return
        rows = self._rows()
        existing_labels = {label for label, _ in rows if label}
        for label in template["fields"]:
            if label not in existing_labels:
                rows.append((label, ""))
        self._set_rows(rows)

    def _on_save(self) -> None:
        owner = self._owner_edit.text().strip() or None
        notes = self._notes_edit.toPlainText().strip() or None
        try:
            self._project.update_parcel(self._parcel_id, owner=owner, notes=notes)
            self._project.set_parcel_fields(self._parcel_id, self._rows())
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return
        self.accept()
