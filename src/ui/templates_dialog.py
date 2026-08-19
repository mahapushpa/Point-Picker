"""templates_dialog — create / edit / delete land-type templates (M10).

Thin UI over ``ProjectDB``'s template methods: all validation and storage live in
``core``/``project_db``; this dialog only gathers input and shows errors. Built-in
templates (the three land types) are listed for reference but are read-only —
they cannot be edited or deleted, matching the built-in unit-profile pattern.

A template is just an ordered list of field *labels*; they are edited here one
per line, which is the simplest faithful representation of "an ordered set of
labels" and keeps the dialog free of per-row widgets.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from ..core.project_db import ProjectError


class TemplatesDialog(QDialog):
    """Manage the project's land-type identification templates."""

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self._project = project
        self.setWindowTitle("Land-type templates")
        self.resize(460, 460)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "A template is a reusable starting set of identification field labels. "
            "Applying it to a parcel copies these labels — editing that parcel never "
            "changes the template. Built-in templates are fixed."))

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list, 1)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. Rural — orchard")
        name_row.addWidget(self._name_edit, 1)
        layout.addLayout(name_row)

        layout.addWidget(QLabel("Field labels (one per line, in order):"))
        self._labels_edit = QPlainTextEdit()
        self._labels_edit.setPlaceholderText("Khasra number\nVillage\nTehsil\nDistrict")
        layout.addWidget(self._labels_edit, 1)

        buttons = QHBoxLayout()
        self._add_btn = QPushButton("Add new")
        self._add_btn.clicked.connect(self._on_add)
        self._update_btn = QPushButton("Update selected")
        self._update_btn.clicked.connect(self._on_update)
        self._delete_btn = QPushButton("Delete selected")
        self._delete_btn.clicked.connect(self._on_delete)
        buttons.addWidget(self._add_btn)
        buttons.addWidget(self._update_btn)
        buttons.addWidget(self._delete_btn)
        layout.addLayout(buttons)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        close_box.accepted.connect(self.accept)
        layout.addWidget(close_box)

        self._reload()

    # -- helpers ------------------------------------------------------------

    def _reload(self, select_id: int | None = None) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for tmpl in self._project.list_templates():
            suffix = "  (built-in)" if tmpl["is_builtin"] else ""
            item = QListWidgetItem(f"{tmpl['name']}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, tmpl)
            self._list.addItem(item)
            if select_id is not None and tmpl["id"] == select_id:
                self._list.setCurrentItem(item)
        self._list.blockSignals(False)
        self._on_selection_changed(self._list.currentItem(), None)

    def _selected_template(self) -> dict | None:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _labels_text(self, template_id: int) -> str:
        full = self._project.get_template(template_id)
        return "\n".join(full["fields"]) if full else ""

    def _parsed_labels(self) -> list[str]:
        return [ln.strip() for ln in self._labels_edit.toPlainText().splitlines() if ln.strip()]

    def _on_selection_changed(self, current, _previous) -> None:
        tmpl = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        editable = tmpl is not None and not tmpl["is_builtin"]
        self._update_btn.setEnabled(editable)
        self._delete_btn.setEnabled(editable)
        # Built-in fields are shown read-only for reference; user ones are editable.
        self._name_edit.setReadOnly(bool(tmpl and tmpl["is_builtin"]))
        self._labels_edit.setReadOnly(bool(tmpl and tmpl["is_builtin"]))
        if tmpl is not None:
            self._name_edit.setText(tmpl["name"])
            self._labels_edit.setPlainText(self._labels_text(tmpl["id"]))

    # -- actions ------------------------------------------------------------

    def _on_add(self) -> None:
        try:
            new_id = self._project.create_template(self._name_edit.text(), self._parsed_labels())
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not add template", str(exc))
            return
        self._reload(select_id=new_id)

    def _on_update(self) -> None:
        tmpl = self._selected_template()
        if tmpl is None or tmpl["is_builtin"]:
            return
        try:
            self._project.update_template(tmpl["id"], name=self._name_edit.text(),
                                          labels=self._parsed_labels())
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not update template", str(exc))
            return
        self._reload(select_id=tmpl["id"])

    def _on_delete(self) -> None:
        tmpl = self._selected_template()
        if tmpl is None or tmpl["is_builtin"]:
            return
        if QMessageBox.question(self, "Delete template",
                                f"Delete the template {tmpl['name']!r}?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self._project.delete_template(tmpl["id"])
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not delete template", str(exc))
            return
        self._reload()
