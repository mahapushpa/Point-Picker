"""unit_profiles_dialog — create / edit / delete local area-unit profiles (M9).

Thin UI over ``ProjectDB``'s unit-profile methods: all validation and storage
live in ``core``/``project_db``; this dialog only gathers input and shows errors.
Built-in units (sq m / sq ft / acre / hectare) are listed for reference but are
read-only — they cannot be edited or deleted.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from ..core.project_db import ProjectError


class UnitProfilesDialog(QDialog):
    """Manage the project's user-defined local area units."""

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self._project = project
        self.setWindowTitle("Unit profiles")
        self.resize(420, 380)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Local area units are a factor to square metres, applied for display "
            "only — SI is always stored and shown too. Built-in units are fixed."))

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list, 1)

        form = QHBoxLayout()
        form.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. Bigha — Jaipur")
        form.addWidget(self._name_edit, 1)
        form.addWidget(QLabel("Square metres per unit:"))
        self._factor_edit = QLineEdit()
        self._factor_edit.setPlaceholderText("e.g. 2529.28")
        self._factor_edit.setMaximumWidth(120)
        form.addWidget(self._factor_edit)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self._add_btn = QPushButton("Add new")
        self._add_btn.setToolTip("Add a new local unit profile from the name and square-metres-per-unit above")
        self._add_btn.clicked.connect(self._on_add)
        self._update_btn = QPushButton("Update selected")
        self._update_btn.setToolTip("Save your edits to the selected profile (built-in units cannot be changed)")
        self._update_btn.clicked.connect(self._on_update)
        self._delete_btn = QPushButton("Delete selected")
        self._delete_btn.setToolTip("Delete the selected local profile (built-in units cannot be deleted)")
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
        for prof in self._project.list_unit_profiles():
            suffix = "  (built-in)" if prof["is_builtin"] else ""
            item = QListWidgetItem(f"{prof['name']} — {prof['sq_m_per_unit']:g} m²{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, prof)
            self._list.addItem(item)
            if select_id is not None and prof["id"] == select_id:
                self._list.setCurrentItem(item)
        self._list.blockSignals(False)
        self._on_selection_changed(self._list.currentItem(), None)

    def _selected_profile(self) -> dict | None:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_selection_changed(self, current, _previous) -> None:
        prof = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        editable = prof is not None and not prof["is_builtin"]
        self._update_btn.setEnabled(editable)
        self._delete_btn.setEnabled(editable)
        if prof is not None:
            self._name_edit.setText(prof["name"])
            self._factor_edit.setText(f"{prof['sq_m_per_unit']:g}")

    def _parse_factor(self) -> float | None:
        text = self._factor_edit.text().strip()
        try:
            value = float(text)
        except ValueError:
            QMessageBox.warning(self, "Invalid factor",
                                "Enter the number of square metres in one unit (e.g. 2529.28).")
            return None
        if not (value > 0):
            QMessageBox.warning(self, "Invalid factor", "The factor must be greater than zero.")
            return None
        return value

    # -- actions ------------------------------------------------------------

    def _on_add(self) -> None:
        factor = self._parse_factor()
        if factor is None:
            return
        try:
            new_id = self._project.create_unit_profile(self._name_edit.text(), factor)
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not add unit", str(exc))
            return
        self._reload(select_id=new_id)

    def _on_update(self) -> None:
        prof = self._selected_profile()
        if prof is None or prof["is_builtin"]:
            return
        factor = self._parse_factor()
        if factor is None:
            return
        try:
            self._project.update_unit_profile(prof["id"], name=self._name_edit.text(),
                                               sq_m_per_unit=factor)
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not update unit", str(exc))
            return
        self._reload(select_id=prof["id"])

    def _on_delete(self) -> None:
        prof = self._selected_profile()
        if prof is None or prof["is_builtin"]:
            return
        if QMessageBox.question(self, "Delete unit",
                                f"Delete the unit {prof['name']!r}?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self._project.delete_unit_profile(prof["id"])
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not delete unit", str(exc))
            return
        self._reload()
