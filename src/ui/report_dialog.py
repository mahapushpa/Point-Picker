"""report_dialog — preview + generate the owner-wise summary report (M11).

Thin UI: it receives an already-built :class:`src.export.report.OwnerReport`
(pure data — no file has been written yet) and shows exactly which owners and
how many parcels the report will cover *before* anything is generated, per the
brief's "show coverage before generating" requirement. The user picks which
formats to write (PDF primary, plus CSV / JSON); the actual writing and the
never-overwrite filename handling live in ``src.export.report`` and the calling
window, not here.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGroupBox, QLabel, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout,
)


def _area(value):
    return "— (no scale)" if value is None else f"{value:.2f} m²"


class OwnerReportDialog(QDialog):
    """Confirm coverage and choose formats for the owner-wise summary."""

    def __init__(self, report, scope_label, parent=None):
        super().__init__(parent)
        self._report = report
        self.setWindowTitle("Generate owner-wise report")
        self.resize(540, 460)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>Scope:</b> {scope_label}"))
        n_groups = len(report.groups)   # owner groups incl. any "(no owner)" bucket
        layout.addWidget(QLabel(
            f"{report.parcel_count} parcel(s) under {report.owner_count} owner(s). "
            f"<b>One file per owner</b> will be produced for each chosen format "
            f"({n_groups} owner group(s) → {n_groups} file(s) per format). "
            "Review coverage below."))

        tree = QTreeWidget()
        tree.setHeaderLabels(["Owner / parcel", "Parcels", "Area"])
        tree.setColumnWidth(0, 300)
        for group in report.groups:
            top = QTreeWidgetItem([
                group.display_owner, str(group.parcel_count),
                _area(group.total_area_sq_m)])
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            for parcel in group.parcels:
                top.addChild(QTreeWidgetItem(
                    [parcel.label, "", _area(parcel.area_sq_m)]))
            tree.addTopLevelItem(top)
            top.setExpanded(True)
        layout.addWidget(tree, 1)

        if report.missing_scale_count:
            warn = QLabel(
                f"⚠ {report.missing_scale_count} parcel(s) have no scale set and "
                "show “(no scale)” — they are excluded from area totals.")
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #A0522D;")
            layout.addWidget(warn)

        # Grand total (SI + derived units) for the whole report.
        layout.addWidget(QLabel(
            "<b>Grand total area:</b> "
            + (_area(report.grand_total_area_sq_m)
               + (f"  ({report.grand_total_area_hectare:.4f} ha, "
                  f"{report.grand_total_area_acre:.4f} acre)"
                  if report.grand_total_area_sq_m is not None else ""))))

        fmt_box = QGroupBox("Formats (written to the project's exports/ folder)")
        fmt_layout = QVBoxLayout(fmt_box)
        fmt_layout.addWidget(QLabel("PDF (primary, shareable) — always generated."))
        self._csv = QCheckBox("Also write CSV (spreadsheet / record-keeping)")
        self._json = QCheckBox("Also write JSON (structured record)")
        for cb in (self._csv, self._json):
            fmt_layout.addWidget(cb)
        layout.addWidget(fmt_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        gen_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        gen_btn.setText("Generate")
        gen_btn.setToolTip("Generate the report(s) in the chosen formats into the project's exports/ folder")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_formats(self) -> list[str]:
        """The chosen export formats, in a stable order. PDF is always written
        (the primary, shareable format); CSV / JSON are opt-in extras."""
        out = ["pdf"]
        if self._csv.isChecked():
            out.append("csv")
        if self._json.isChecked():
            out.append("json")
        return out
