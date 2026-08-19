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

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from ..core.project_db import ProjectError


class IdentificationDialog(QDialog):
    """Edit one parcel's identification fields, owner, and notes."""

    def __init__(self, project, parcel_id: int, parent=None):
        super().__init__(parent)
        self._project = project
        self._parcel_id = parcel_id
        self.setWindowTitle("Parcel identification")
        self.resize(460, 520)

        parcel = project.get_parcel(parcel_id) or {}

        layout = QVBoxLayout(self)

        # Always present: the source-document reference (read-only).
        layout.addWidget(QLabel(self._source_reference_text(parcel.get("source_id"))))

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
        add_btn.clicked.connect(lambda: self._append_row("", ""))
        remove_btn = QPushButton("Remove selected")
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

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                               QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._on_save)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

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
