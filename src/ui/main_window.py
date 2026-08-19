"""main_window — application shell (PySide6).

Thin UI: decode files via ``src.io``, drive interaction through the canvas, and
delegate all arithmetic to ``src.core`` (scale, geometry) and all storage to
``src.core.project_db``. This window only wires them together and renders
results; it holds no domain logic.

Milestone 4 scope:
  * project-awareness — New/Open Project, register the loaded source into the
    project (copied into ``sources/``, referenced by relative path), and persist
    the scale and traced boundary to ``project.db``;
  * polygon tracing — click a sequence of boundary points, live segment /
    perimeter / area readout, undo / close / clear.

No-scale behaviour (chosen): tracing is allowed with **no** scale set — the
readout shows pixel units with an explicit "(no scale)" indicator, and switches
to metres/square-metres the moment a scale is set (the boundary is re-measured
and re-saved). This is friendlier than forcing scale-first and lets a user trace
now and calibrate later.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDockWidget, QFileDialog, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QToolBar, QVBoxLayout, QWidget,
)

from ..core.geometry import measure_polygon
from ..core.project_db import ProjectDB, ProjectError
from ..core.scale import compute_two_point_scale, TwoPointScale
from ..core.selection import parcel_at_point, parcels_in_rect
from ..core import units
from ..io.raster import open_raster
from ..io.preprocess import preprocess_raster
from .canvas_view import CanvasView
from .unit_profiles_dialog import UnitProfilesDialog
from .templates_dialog import TemplatesDialog
from .identification_dialog import IdentificationDialog

#: Stable, visually-distinct colours assigned to parcels by their position, so
#: several boundaries on one sheet don't get confused. Avoids the green used by
#: scale-calibration markers.
_PARCEL_PALETTE = (
    "#E8770F", "#2D7DD2", "#8E44AD", "#16A085",
    "#C0392B", "#B8860B", "#D6336C", "#4B6584",
)


def _parcel_color(index: int) -> QColor:
    return QColor(_PARCEL_PALETTE[index % len(_PARCEL_PALETTE)])

_FILE_FILTER = (
    "Supported documents (*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff);;"
    "PDF (*.pdf);;"
    "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff);;"
    "All files (*)"
)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Land Measure Tool")
        self.resize(1100, 760)

        self.canvas = CanvasView(self)
        self.setCentralWidget(self.canvas)
        self.canvas.twoPointsPicked.connect(self._on_two_points_picked)
        self.canvas.polygonChanged.connect(self._on_polygon_changed)
        self.canvas.polygonClosed.connect(self._on_polygon_closed)
        self.canvas.vertexMoved.connect(self._on_vertex_moved)
        self.canvas.selectionClicked.connect(self._on_selection_clicked)
        self.canvas.marqueeSelected.connect(self._on_marquee_selected)

        # Session state.
        self._project: ProjectDB | None = None
        self._current_path: str | None = None
        self._source_id: int | None = None
        self._scale: TwoPointScale | None = None  # in-memory; mirrors the DB when a project is open
        self._parcels: list[dict] = []            # parcels of the current source (project mode)
        self._active_parcel_id: int | None = None
        # A working subset of parcels, separate from the single active parcel.
        # Session-only (a scratch working set): never persisted, cleared on
        # opening a project / loading another file.
        self._selected_parcel_ids: set[int] = set()

        # Image preprocessing (M8): a display-time, non-destructive preview. The
        # raw raster and the source file are never modified; the enhanced raster
        # is computed on demand and cached. Pixel coordinates are unchanged, so
        # scale/tracing/snapping work identically whichever is shown.
        self._raw_raster = None
        self._pre_raster = None       # cached preprocessed raster (lazily built)
        self._preprocess_on = False

        # Status bar: transient message on the left; permanent readouts right.
        self._status = QLabel("Open a PDF or image to begin.")
        self.statusBar().addWidget(self._status)
        self._project_readout = QLabel("No project (unsaved)")
        self._scale_readout = QLabel("Scale: not set")
        self._measure_readout = QLabel("No boundary")
        for w in (self._project_readout, self._scale_readout, self._measure_readout):
            self.statusBar().addPermanentWidget(w)

        self._build_menu()
        self._build_toolbar()
        self._build_units_toolbar()
        self._build_parcel_dock()

    # -- construction -------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = QToolBar("Tools", self)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)

        for text, tooltip, slot in (
            ("−", "Zoom out", self.canvas.zoom_out),
            ("Reset", "Reset view (fit to window)", self.canvas.reset_view),
            ("+", "Zoom in", self.canvas.zoom_in),
        ):
            act = QAction(text, self)
            act.setToolTip(tooltip)
            act.triggered.connect(slot)
            bar.addAction(act)

        bar.addSeparator()
        self._preprocess_btn = QAction("Enhance", self)
        self._preprocess_btn.setCheckable(True)
        self._preprocess_btn.setToolTip("Preview denoise + contrast enhancement (non-destructive)")
        self._preprocess_btn.toggled.connect(self.set_preprocess_enabled)
        bar.addAction(self._preprocess_btn)

        bar.addSeparator()
        set_scale = QAction("Set scale", self)
        set_scale.setToolTip("Set scale: click two points a known distance apart")
        set_scale.triggered.connect(self.begin_scale_calibration)
        bar.addAction(set_scale)

        bar.addSeparator()
        for text, tooltip, slot in (
            ("Trace", "Trace boundary: click points to build a polygon", self.begin_polygon_tracing),
            ("Undo pt", "Remove the last boundary point", self.canvas.undo_last_point),
            ("Close", "Close the boundary (needs 3+ points)", self.close_polygon),
            ("Clear", "Clear the traced boundary", self.clear_polygon),
        ):
            act = QAction(text, self)
            act.setToolTip(tooltip)
            act.triggered.connect(slot)
            bar.addAction(act)

        bar.addSeparator()
        # A checkable, clearly-labelled toggle so Select mode stands out from the
        # one-shot Scale/Trace actions and visibly latches while it is active —
        # the M7 mechanism was correct but users couldn't find how to enter it.
        self._select_action = QAction("Select parcels", self)
        self._select_action.setCheckable(True)
        self._select_action.setToolTip(
            "Select parcels (toggle): click a parcel to add/remove it, drag a "
            "marquee to select several. A working set, separate from the parcel "
            "being edited.")
        self._select_action.toggled.connect(self._on_select_toggled)
        bar.addAction(self._select_action)

    def _build_units_toolbar(self) -> None:
        """A visible 'Display units' row: pick the local area unit shown alongside
        SI for the current source, and manage the saved profiles. On its own
        toolbar so it's easy to find (units are a per-source display choice)."""
        bar = QToolBar("Units", self)
        bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        bar.addWidget(QLabel(" Display units: "))
        self._unit_combo = QComboBox()
        self._unit_combo.setToolTip(
            "Local area unit shown next to SI for this source's parcel areas. "
            "SI (square metres) is always shown as the verifiable baseline.")
        self._unit_combo.setMinimumWidth(180)
        self._unit_combo.currentIndexChanged.connect(self._on_unit_selected)
        bar.addWidget(self._unit_combo)
        manage = QAction("Manage units…", self)
        manage.setToolTip("Create, edit, or delete local area-unit profiles")
        manage.triggered.connect(self.manage_unit_profiles)
        bar.addAction(manage)
        self._refresh_unit_combo()

    def _build_parcel_dock(self) -> None:
        """Sidebar listing the current source's parcels: select the active one,
        add/delete, and edit its owner. Only meaningful with a project open."""
        dock = QDockWidget("Parcels", self)
        dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea |
                             Qt.DockWidgetArea.RightDockWidgetArea)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(6, 6, 6, 6)

        self._parcel_list = QListWidget()
        # Current row = the active (editable) parcel; the checkbox = membership of
        # the selection working set. Two independent states in one list.
        self._parcel_list.currentRowChanged.connect(self._on_parcel_row_changed)
        self._parcel_list.itemChanged.connect(self._on_parcel_item_changed)
        layout.addWidget(self._parcel_list, 1)

        buttons = QHBoxLayout()
        self._new_parcel_btn = QPushButton("New parcel")
        self._new_parcel_btn.clicked.connect(self.new_parcel)
        self._del_parcel_btn = QPushButton("Delete")
        self._del_parcel_btn.clicked.connect(self.delete_active_parcel)
        buttons.addWidget(self._new_parcel_btn)
        buttons.addWidget(self._del_parcel_btn)
        layout.addLayout(buttons)

        # Selection working-set controls (Milestone 7).
        sel_buttons = QHBoxLayout()
        self._select_all_btn = QPushButton("Select all")
        self._select_all_btn.clicked.connect(self.select_all_parcels)
        self._clear_sel_btn = QPushButton("Clear selection")
        self._clear_sel_btn.clicked.connect(self.clear_selection)
        sel_buttons.addWidget(self._select_all_btn)
        sel_buttons.addWidget(self._clear_sel_btn)
        layout.addLayout(sel_buttons)
        self._selection_label = QLabel("No parcels selected")
        layout.addWidget(self._selection_label)

        layout.addWidget(QLabel("Owner:"))
        self._owner_edit = QLineEdit()
        self._owner_edit.setPlaceholderText("owner name")
        self._owner_edit.editingFinished.connect(self._on_owner_edited)
        layout.addWidget(self._owner_edit)

        # Identification / revenue-record fields for the active parcel (M10).
        self._ident_btn = QPushButton("Identification…")
        self._ident_btn.setToolTip("Edit this parcel's identification fields, apply a "
                                   "land-type template, and edit notes")
        self._ident_btn.clicked.connect(self.edit_identification)
        layout.addWidget(self._ident_btn)

        dock.setWidget(body)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._parcel_dock = dock
        self._refresh_parcel_controls_enabled()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        new_proj = QAction("&New Project…", self)
        new_proj.triggered.connect(self.new_project)
        file_menu.addAction(new_proj)
        open_proj = QAction("Open &Project…", self)
        open_proj.triggered.connect(self.open_project)
        file_menu.addAction(open_proj)
        file_menu.addSeparator()
        open_act = QAction("&Open File…", self)
        open_act.setShortcut(QKeySequence.StandardKey.Open)
        open_act.triggered.connect(self.open_file_dialog)
        file_menu.addAction(open_act)
        file_menu.addSeparator()
        quit_act = QAction("&Quit", self)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        view_menu = self.menuBar().addMenu("&View")
        for text, slot, shortcut in (
            ("Zoom &In", self.canvas.zoom_in, QKeySequence.StandardKey.ZoomIn),
            ("Zoom &Out", self.canvas.zoom_out, QKeySequence.StandardKey.ZoomOut),
            ("&Reset View", self.canvas.reset_view, "Ctrl+0"),
        ):
            act = QAction(text, self)
            act.setShortcut(shortcut)
            act.triggered.connect(slot)
            view_menu.addAction(act)
        view_menu.addSeparator()
        self._crosshair_action = QAction("Precision &crosshair", self, checkable=True)
        self._crosshair_action.setToolTip("Full-window crosshair while picking points")
        self._crosshair_action.toggled.connect(self.canvas.set_crosshair_enabled)
        view_menu.addAction(self._crosshair_action)
        view_menu.addSeparator()
        self._preprocess_action = QAction("&Preprocess scan (preview)", self, checkable=True)
        self._preprocess_action.setToolTip(
            "Display-time denoise + contrast enhancement — non-destructive, "
            "does not change pixel coordinates")
        self._preprocess_action.toggled.connect(self.set_preprocess_enabled)
        view_menu.addAction(self._preprocess_action)

        scale_menu = self.menuBar().addMenu("&Scale")
        set_scale_act = QAction("&Set scale (two points)…", self)
        set_scale_act.triggered.connect(self.begin_scale_calibration)
        scale_menu.addAction(set_scale_act)
        clear_scale_act = QAction("&Clear scale", self)
        clear_scale_act.triggered.connect(self.clear_scale)
        scale_menu.addAction(clear_scale_act)

        units_menu = self.menuBar().addMenu("&Units")
        manage_units_act = QAction("&Manage unit profiles…", self)
        manage_units_act.triggered.connect(self.manage_unit_profiles)
        units_menu.addAction(manage_units_act)

        records_menu = self.menuBar().addMenu("&Records")
        ident_act = QAction("Edit &identification fields…", self)
        ident_act.triggered.connect(self.edit_identification)
        records_menu.addAction(ident_act)
        manage_tmpl_act = QAction("Manage &templates…", self)
        manage_tmpl_act.triggered.connect(self.manage_templates)
        records_menu.addAction(manage_tmpl_act)

        select_menu = self.menuBar().addMenu("Se&lection")
        select_act = QAction("&Select parcels (click / marquee)", self)
        select_act.triggered.connect(self.begin_selection)
        select_menu.addAction(select_act)
        select_menu.addSeparator()
        select_all_act = QAction("Select &all parcels", self)
        select_all_act.triggered.connect(self.select_all_parcels)
        select_menu.addAction(select_all_act)
        clear_sel_act = QAction("&Clear selection", self)
        clear_sel_act.triggered.connect(self.clear_selection)
        select_menu.addAction(clear_sel_act)

        poly_menu = self.menuBar().addMenu("&Boundary")
        trace_act = QAction("&Trace boundary", self)
        trace_act.triggered.connect(self.begin_polygon_tracing)
        poly_menu.addAction(trace_act)
        undo_act = QAction("&Undo last point", self)
        undo_act.setShortcut(QKeySequence.StandardKey.Undo)
        undo_act.triggered.connect(self.canvas.undo_last_point)
        poly_menu.addAction(undo_act)
        close_poly_act = QAction("&Close boundary", self)
        close_poly_act.triggered.connect(self.close_polygon)
        poly_menu.addAction(close_poly_act)
        clear_poly_act = QAction("C&lear boundary", self)
        clear_poly_act.triggered.connect(self.clear_polygon)
        poly_menu.addAction(clear_poly_act)

    # -- projects -----------------------------------------------------------

    def new_project(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose an empty folder for the new project")
        if not folder:
            return
        try:
            proj = ProjectDB.create(folder)
        except ProjectError as exc:
            QMessageBox.warning(
                self, "Could not create project",
                f"{exc}\n\nUse 'Open Project' to open an existing one.")
            return
        self._set_project(proj)
        self._attach_source_to_project()
        self._status.setText(f"Created project at {proj.root}")

    def open_project(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Open an existing project folder")
        if not folder:
            return
        try:
            proj = ProjectDB.open(folder)
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not open project", str(exc))
            return
        self._set_project(proj)
        self._attach_source_to_project()
        self._status.setText(f"Opened project at {proj.root}")

    def _set_project(self, proj: ProjectDB) -> None:
        if self._project is not None:
            self._project.close()
        self._project = proj
        self._source_id = None
        self._parcels = []
        self._active_parcel_id = None
        self._selected_parcel_ids.clear()   # selection is a per-session working set
        self._project_readout.setText(f"Project: {proj.get_meta('project_name')}")
        self._refresh_unit_combo()   # profiles are project-level
        self._update_title()

    def _attach_source_to_project(self) -> None:
        """Register the loaded file into the open project (idempotent), restore
        its saved scale, and load its parcels — or, for a source not seen before,
        push the current in-memory scale/boundary in as its first parcel."""
        if self._project is None or self._current_path is None:
            self._reload_parcels()
            return
        try:
            sid, _existed = self._project.import_or_get_source(self._current_path)
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not register file", str(exc))
            return
        self._source_id = sid

        db_scale = self._project.get_source_scale(sid)
        if db_scale is not None:
            self._scale = _scale_from_db_row(db_scale)  # saved scale wins
        elif self._scale is not None:
            self._persist_scale()                       # push in-memory scale

        parcels = self._project.list_parcels(sid)
        if not parcels and self.canvas.polygon_points():
            # First time this file is added and something is already traced:
            # keep that work as parcel 1.
            pid = self._project.create_parcel(sid)
            self._project.save_parcel_polygon(
                pid, self.canvas.polygon_points(),
                closed=self.canvas.is_polygon_closed(),
                metres_per_pixel=self._mpp())
            parcels = self._project.list_parcels(sid)

        self._parcels = parcels
        active = parcels[0]["id"] if parcels else None
        self._reload_parcels(select_id=active)
        self._refresh_unit_combo()   # enable + reflect this source's active unit
        self._update_scale_readout()

    # -- parcels ------------------------------------------------------------

    def _reload_parcels(self, select_id: int | None = None) -> None:
        """Refresh the parcel list from the DB and set the active parcel (loading
        its boundary into the canvas and the others into the background)."""
        if self._project is not None and self._source_id is not None:
            self._parcels = self._project.list_parcels(self._source_id)
        else:
            self._parcels = []
        self._rebuild_parcel_list()

        if select_id is None:
            select_id = self._active_parcel_id
        ids = [p["id"] for p in self._parcels]
        if select_id not in ids:
            select_id = ids[0] if ids else None
        self._set_active_parcel(select_id)
        self._update_selection_ui()   # prune stale ids, sync checkboxes + count
        self._refresh_parcel_controls_enabled()

    def _rebuild_parcel_list(self) -> None:
        self._parcel_list.blockSignals(True)
        self._parcel_list.clear()
        for i, p in enumerate(self._parcels):
            item = QListWidgetItem(self._parcel_label(i, p))
            item.setForeground(_parcel_color(i))  # colour matches the on-canvas boundary
            item.setData(Qt.ItemDataRole.UserRole, p["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if p["id"] in self._selected_parcel_ids
                               else Qt.CheckState.Unchecked)
            self._parcel_list.addItem(item)
        self._parcel_list.blockSignals(False)

    def _parcel_label(self, index: int, parcel: dict) -> str:
        owner = parcel.get("owner") or "(no owner)"
        n = parcel.get("point_count", 0)
        mark = " ✓" if parcel.get("closed") else ""
        return f"{index + 1}. {owner} — {n} pt{'s' if n != 1 else ''}{mark}"

    def _set_active_parcel(self, parcel_id: int | None) -> None:
        self._active_parcel_id = parcel_id
        if parcel_id is None:
            self.canvas.set_polygon([], closed=False)
            self.canvas.set_background_polygons([])
            self._refresh_snap_vertices()
            self._owner_edit.blockSignals(True)
            self._owner_edit.setText("")
            self._owner_edit.blockSignals(False)
            self._update_measure_readout()
            return

        index = self._parcel_index(parcel_id)
        self.canvas.set_active_color(_parcel_color(index if index is not None else 0))
        pts = self._project.get_parcel_polygon(parcel_id)
        vids = self._project.get_parcel_vertex_ids(parcel_id)
        self.canvas.set_polygon(pts, closed=self._project.get_parcel_closed(parcel_id),
                                vertex_ids=vids)
        self._refresh_snap_vertices()
        self._refresh_backgrounds()

        parcel = self._parcels[index] if index is not None else self._project.get_parcel(parcel_id)
        self._owner_edit.blockSignals(True)
        self._owner_edit.setText((parcel.get("owner") or "") if parcel else "")
        self._owner_edit.blockSignals(False)

        # Keep the list selection in sync (e.g. when set programmatically).
        if index is not None and self._parcel_list.currentRow() != index:
            self._parcel_list.blockSignals(True)
            self._parcel_list.setCurrentRow(index)
            self._parcel_list.blockSignals(False)
        self._update_measure_readout()

    def _refresh_backgrounds(self) -> None:
        """Draw every parcel except the active one as a context overlay, carrying
        vertex ids so a shared vertex moves in lock-step when edited."""
        polys = []
        for i, p in enumerate(self._parcels):
            if p["id"] == self._active_parcel_id:
                continue
            pts = self._project.get_parcel_polygon(p["id"])
            vids = self._project.get_parcel_vertex_ids(p["id"])
            polys.append((p["id"], pts, vids, bool(p["closed"]), _parcel_color(i), f"{i + 1}"))
        self.canvas.set_background_polygons(polys)
        self.canvas.set_selected_ids(self._selected_parcel_ids)

    def _refresh_snap_vertices(self) -> None:
        """Give the canvas the source's vertices so new points snap onto shared
        ones (the canvas excludes the active parcel's own vertices)."""
        if self._project is None or self._source_id is None:
            self.canvas.set_snap_vertices([])
            return
        self.canvas.set_snap_vertices(
            [(v["id"], v["pixel_x"], v["pixel_y"]) for v in self._project.list_vertices(self._source_id)])

    def new_parcel(self) -> None:
        if self._project is None or self._source_id is None:
            QMessageBox.information(
                self, "No project",
                "Open or create a project first — parcels are saved per project.")
            return
        pid = self._project.create_parcel(self._source_id, owner=self._owner_edit.text().strip() or None)
        self._reload_parcels(select_id=pid)
        self.begin_polygon_tracing()  # ready to trace the new parcel immediately
        self._status.setText(f"New parcel {len(self._parcels)} — click to trace its boundary.")

    def delete_active_parcel(self) -> None:
        if self._project is None or self._active_parcel_id is None:
            return
        index = self._parcel_index(self._active_parcel_id)
        label = self._parcel_label(index or 0, self._parcels[index]) if index is not None else "this parcel"
        if QMessageBox.question(self, "Delete parcel", f"Delete {label}?") != QMessageBox.StandardButton.Yes:
            return
        self._project.delete_parcel(self._active_parcel_id)
        self._active_parcel_id = None
        self._reload_parcels()
        self._status.setText("Parcel deleted.")

    def _on_parcel_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._parcels):
            return
        self._set_active_parcel(self._parcels[row]["id"])

    def _on_owner_edited(self) -> None:
        if self._project is None or self._active_parcel_id is None:
            return
        owner = self._owner_edit.text().strip() or None
        self._project.update_parcel(self._active_parcel_id, owner=owner)
        index = self._parcel_index(self._active_parcel_id)
        if index is not None:
            self._parcels[index]["owner"] = owner
            self._update_parcel_list_row(index)

    def _refresh_parcel_controls_enabled(self) -> None:
        has_project = self._project is not None and self._source_id is not None
        has_parcels = has_project and bool(self._parcels)
        self._new_parcel_btn.setEnabled(has_project)
        self._del_parcel_btn.setEnabled(has_project and self._active_parcel_id is not None)
        self._owner_edit.setEnabled(has_project and self._active_parcel_id is not None)
        self._ident_btn.setEnabled(has_project and self._active_parcel_id is not None)
        self._select_all_btn.setEnabled(has_parcels)
        self._clear_sel_btn.setEnabled(has_parcels and bool(self._selected_parcel_ids))

    # -- parcel selection (Milestone 7) -------------------------------------

    def begin_selection(self) -> None:
        """Enter parcel selection mode (menu / programmatic entry point). Latches
        the checkable toolbar toggle, which drives :meth:`_on_select_toggled`."""
        if self._select_action.isChecked():
            self._on_select_toggled(True)   # already on: (re)affirm the mode + hint
        else:
            self._select_action.setChecked(True)  # emits toggled -> _on_select_toggled

    def _on_select_toggled(self, checked: bool) -> None:
        """The Select toolbar toggle changed. On: enter canvas selection mode and
        explain it in the status bar. Off: leave selection mode (the selection
        set itself is kept)."""
        if checked:
            if not self.canvas.start_selection():
                self._set_select_checked(False)
                QMessageBox.information(self, "No document", "Open a PDF or image first.")
                return
            self._sync_crosshair_action()
            self._status.setText(
                "Select mode: click a parcel to toggle it, drag to select several. "
                "(This working set is separate from the parcel being edited.)")
        else:
            if self.canvas.is_selecting():
                self.canvas.stop_selection()
            self._status.setText("Select mode off.")

    def _set_select_checked(self, on: bool) -> None:
        """Set the Select toggle's visual state without re-running its handler
        (used when another mode takes over, or to revert a failed entry)."""
        self._select_action.blockSignals(True)
        self._select_action.setChecked(on)
        self._select_action.blockSignals(False)

    def selected_parcel_ids(self) -> list[int]:
        """The current selection working subset, in parcel (display) order. This
        is the hook later milestones (location-fixing, report scoping) consume."""
        return [p["id"] for p in self._parcels if p["id"] in self._selected_parcel_ids]

    def set_parcel_selected(self, parcel_id: int, selected: bool) -> None:
        if selected:
            self._selected_parcel_ids.add(parcel_id)
        else:
            self._selected_parcel_ids.discard(parcel_id)
        self._update_selection_ui()

    def toggle_parcel_selection(self, parcel_id: int) -> None:
        self.set_parcel_selected(parcel_id, parcel_id not in self._selected_parcel_ids)

    def select_all_parcels(self) -> None:
        self._selected_parcel_ids = {p["id"] for p in self._parcels}
        self._update_selection_ui()

    def clear_selection(self) -> None:
        self._selected_parcel_ids.clear()
        self._update_selection_ui()

    def _on_selection_clicked(self, scene_pt) -> None:
        """A click in selection mode: toggle the parcel under the cursor (if any)."""
        parcels = [(p["id"], self._project.get_parcel_polygon(p["id"])) for p in self._parcels] \
            if self._project is not None else []
        pid = parcel_at_point(parcels, (scene_pt.x(), scene_pt.y()))
        if pid is not None:
            self.toggle_parcel_selection(pid)

    def _on_marquee_selected(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """A marquee drag in selection mode: add every parcel it touches to the
        selection (additive; click-toggle removes individual ones)."""
        if self._project is None:
            return
        parcels = [(p["id"], self._project.get_parcel_polygon(p["id"])) for p in self._parcels]
        hit = parcels_in_rect(parcels, (x0, y0, x1, y1))
        if hit:
            self._selected_parcel_ids.update(hit)
            self._update_selection_ui()

    def _on_parcel_item_changed(self, item) -> None:
        """Sidebar checkbox toggled: mirror it into the selection set (independent
        of which parcel is active)."""
        pid = item.data(Qt.ItemDataRole.UserRole)
        if pid is None:
            return
        selected = item.checkState() == Qt.CheckState.Checked
        if selected == (pid in self._selected_parcel_ids):
            return  # no real change (e.g. programmatic refresh)
        self.set_parcel_selected(pid, selected)

    def _update_selection_ui(self) -> None:
        """Reflect the selection set everywhere: the count label, the sidebar
        checkboxes, and the canvas highlight — without touching the active parcel."""
        # Drop ids that no longer exist (e.g. after a delete).
        live = {p["id"] for p in self._parcels}
        self._selected_parcel_ids &= live

        n = len(self._selected_parcel_ids)
        self._selection_label.setText(
            "No parcels selected" if n == 0
            else f"{n} parcel{'s' if n != 1 else ''} selected")

        self._parcel_list.blockSignals(True)
        for row in range(self._parcel_list.count()):
            it = self._parcel_list.item(row)
            pid = it.data(Qt.ItemDataRole.UserRole)
            want = Qt.CheckState.Checked if pid in self._selected_parcel_ids else Qt.CheckState.Unchecked
            if it.checkState() != want:
                it.setCheckState(want)
        self._parcel_list.blockSignals(False)

        self.canvas.set_selected_ids(self._selected_parcel_ids)
        self._clear_sel_btn.setEnabled(bool(self._selected_parcel_ids))

    def _parcel_index(self, parcel_id: int | None) -> int | None:
        for i, p in enumerate(self._parcels):
            if p["id"] == parcel_id:
                return i
        return None

    def _update_parcel_list_row(self, index: int) -> None:
        item = self._parcel_list.item(index)
        if item is not None:
            item.setText(self._parcel_label(index, self._parcels[index]))

    def _mpp(self) -> float | None:
        return self._scale.metres_per_pixel if self._scale is not None else None

    # -- files --------------------------------------------------------------

    def open_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open document", "", _FILE_FILTER)
        if path:
            self.load_path(path)

    def load_path(self, path) -> None:
        """Decode *path* and show it. If a project is open, register the file and
        restore any saved scale/boundary for it."""
        try:
            raster = open_raster(path)
        except Exception as exc:  # decoding errors are user-facing, not crashes
            QMessageBox.critical(self, "Could not open file", f"{Path(path).name}\n\n{exc}")
            self._status.setText("Open a PDF or image to begin.")
            return
        self.canvas.set_image(raster)   # clears any prior markers/boundary + exits selection mode
        self._set_select_checked(False)
        self._raw_raster = raster
        self._pre_raster = None         # invalidate any cached enhancement
        self._current_path = str(path)
        self._source_id = None
        self._scale = None              # a new file has its own scale/boundaries
        self._parcels = []
        self._active_parcel_id = None
        self._selected_parcel_ids.clear()   # selection is per-source; start fresh
        self._apply_display_image()     # honour the preprocessing toggle for the new file
        self._attach_source_to_project()
        self._update_scale_readout()
        self._update_measure_readout()
        self._status.setText(f"{Path(path).name}   —   {raster.width} × {raster.height} px")
        self._update_title()

    # -- image preprocessing (Milestone 8) ----------------------------------

    def set_preprocess_enabled(self, enabled: bool) -> None:
        """Toggle the display-time denoise+contrast preview. Non-destructive: the
        raw raster and source file are untouched, and pixel coordinates are
        unchanged, so scale/tracing/snapping behave identically either way."""
        enabled = bool(enabled)
        self._preprocess_on = enabled
        self._sync_preprocess_controls()
        if self._raw_raster is None:
            if enabled:
                QMessageBox.information(self, "No document", "Open a PDF or image first.")
                self._preprocess_on = False
                self._sync_preprocess_controls()
            return
        self._apply_display_image()
        self._status.setText(
            "Showing enhanced scan (denoise + contrast) — display only."
            if self._preprocess_on else "Showing original scan.")

    def _apply_display_image(self) -> None:
        """Push the raw or enhanced pixels to the canvas per the current toggle,
        without disturbing any markers, boundary, selection, or zoom."""
        if self._raw_raster is None:
            return
        if self._preprocess_on:
            if self._pre_raster is None:
                self._pre_raster = preprocess_raster(self._raw_raster)  # cache
            self.canvas.set_display_pixels(self._pre_raster)
        else:
            self.canvas.set_display_pixels(self._raw_raster)

    def _sync_preprocess_controls(self) -> None:
        for ctrl in (self._preprocess_action, self._preprocess_btn):
            ctrl.blockSignals(True)
            ctrl.setChecked(self._preprocess_on)
            ctrl.blockSignals(False)

    # -- scale calibration --------------------------------------------------

    def begin_scale_calibration(self) -> None:
        if not self.canvas.start_scale_calibration():
            QMessageBox.information(self, "No document", "Open a PDF or image first.")
            return
        self._set_select_checked(False)   # canvas already left selection mode
        self._sync_crosshair_action()
        self._status.setText(
            "Set scale: click two points a known distance apart; drag or arrow-keys "
            "to fine-tune, Enter to confirm, Esc to cancel.")

    def clear_scale(self) -> None:
        self.canvas.cancel_scale_calibration()
        self._scale = None
        if self._project is not None and self._source_id is not None:
            self._project.clear_source_scale(self._source_id)
            self._project.refresh_source_vertices_si(self._source_id, None)  # drop SI coords
        self._update_scale_readout()
        self._update_measure_readout()
        self._status.setText("Scale cleared.")

    def _on_two_points_picked(self, p1, p2) -> None:
        distance, ok = QInputDialog.getDouble(
            self, "Real-world distance",
            "Distance between the two points (metres):",
            1.0, 0.0000001, 1_000_000.0, 4,
        )
        if not ok:
            self.canvas.clear_scale_markers()
            self._status.setText("Scale calibration cancelled.")
            return
        try:
            scale = compute_two_point_scale((p1.x(), p1.y()), (p2.x(), p2.y()), distance)
        except ValueError as exc:
            QMessageBox.warning(self, "Could not set scale", str(exc))
            self.canvas.clear_scale_markers()
            self._status.setText("Scale not set — pick two distinct points.")
            return
        self._scale = scale
        self._persist_scale()          # store scale + refresh boundary SI coords
        self._update_scale_readout()
        self._update_measure_readout()  # readout switches from px to metres
        self._status.setText(f"Scale set from {scale.pixel_distance:.1f} px = {distance:g} m.")

    # -- polygon tracing ----------------------------------------------------

    def begin_polygon_tracing(self) -> None:
        if not self.canvas.start_polygon():
            QMessageBox.information(self, "No document", "Open a PDF or image first.")
            return
        self._set_select_checked(False)   # canvas already left selection mode
        self._sync_crosshair_action()
        hint = ("Trace boundary: click to add points; drag or arrow-keys to fine-tune; "
                "Enter (or 'Close') to finish, Esc to cancel.")
        if self._scale is None:
            hint += "  (No scale set — measurements in pixels.)"
        self._status.setText(hint)

    def _sync_crosshair_action(self) -> None:
        """Reflect the canvas's current crosshair state (on for scale, off for
        polygon by default) in the checkable menu item, without re-triggering it."""
        self._crosshair_action.blockSignals(True)
        self._crosshair_action.setChecked(self.canvas.is_crosshair_enabled())
        self._crosshair_action.blockSignals(False)

    def close_polygon(self) -> None:
        if not self.canvas.close_polygon():
            if len(self.canvas.polygon_points()) < 3:
                QMessageBox.information(self, "Not enough points",
                                       "A boundary needs at least 3 points to close.")

    def clear_polygon(self) -> None:
        self.canvas.clear_polygon()
        self._status.setText("Boundary cleared.")

    def _on_polygon_changed(self) -> None:
        self._persist_polygon()
        self._update_measure_readout()

    def _on_polygon_closed(self) -> None:
        self._status.setText("Boundary closed.")

    # -- persistence helpers ------------------------------------------------

    def _persist_scale(self) -> None:
        if self._project is None or self._source_id is None or self._scale is None:
            return
        s = self._scale
        self._project.set_source_scale(
            self._source_id, s.metres_per_pixel, method=s.method,
            p1=s.p1, p2=s.p2, real_distance_m=s.real_distance_m,
        )
        # Every parcel shares the source's vertices, so one pass over the source's
        # vertices refreshes all SI coords (pixels unchanged; no re-snapping).
        self._project.refresh_source_vertices_si(self._source_id, s.metres_per_pixel)

    def _persist_polygon(self) -> None:
        """Persist the active boundary to its parcel. If tracing began before a
        parcel existed, lazily create one — without disturbing the in-progress
        canvas state (so tracing continues uninterrupted)."""
        if self._project is None or self._source_id is None:
            return
        pts = self.canvas.polygon_points()
        closed = self.canvas.is_polygon_closed()
        mpp = self._mpp()

        if self._active_parcel_id is None:
            if not pts:
                return
            self._active_parcel_id = self._project.create_parcel(self._source_id)
            self._project.save_parcel_polygon(self._active_parcel_id, pts,
                                              closed=closed, metres_per_pixel=mpp)
            self.canvas.set_active_vertex_ids(
                self._project.get_parcel_vertex_ids(self._active_parcel_id))
            self._refresh_snap_vertices()
            self._parcels = self._project.list_parcels(self._source_id)
            self._rebuild_parcel_list()
            index = self._parcel_index(self._active_parcel_id)
            if index is not None:
                self._parcel_list.blockSignals(True)
                self._parcel_list.setCurrentRow(index)
                self._parcel_list.blockSignals(False)
                self.canvas.set_active_color(_parcel_color(index))
            self._refresh_parcel_controls_enabled()
            return

        self._project.save_parcel_polygon(self._active_parcel_id, pts,
                                          closed=closed, metres_per_pixel=mpp)
        # Re-sync vertex ids (new points became vertices; snapping may have
        # reused others) and the snap set, without disturbing the canvas.
        self.canvas.set_active_vertex_ids(
            self._project.get_parcel_vertex_ids(self._active_parcel_id))
        self._refresh_snap_vertices()
        index = self._parcel_index(self._active_parcel_id)
        if index is not None:
            self._parcels[index]["point_count"] = len(pts)
            self._parcels[index]["closed"] = 1 if closed else 0
            self._update_parcel_list_row(index)

    def _on_vertex_moved(self, vertex_id: int, x: float, y: float) -> None:
        """A shared vertex was dragged/nudged: move it once, for every parcel
        that references it. The canvas already moved the active + background
        drawings; here we persist and refresh the active measurement."""
        if self._project is None or self._source_id is None:
            self._update_measure_readout()
            return
        self._project.move_vertex(vertex_id, x, y, metres_per_pixel=self._mpp())
        self._update_measure_readout()

    # -- unit profiles (Milestone 9) ----------------------------------------

    def _refresh_unit_combo(self) -> None:
        """Rebuild the display-units combo from the project's profiles and select
        the current source's active one (or 'SI only'). Disabled with no source."""
        combo = self._unit_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("SI only (square metre)", None)   # userData None = no profile
        profiles = self._project.list_unit_profiles() if self._project is not None else []
        for prof in profiles:
            combo.addItem(f"{prof['name']}  (1 = {prof['sq_m_per_unit']:g} m²)", prof["id"])
        active = (self._project.get_source_unit_profile(self._source_id)
                  if self._project is not None and self._source_id is not None else None)
        target_id = active["id"] if active else None
        idx = combo.findData(target_id)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)
        combo.setEnabled(self._project is not None and self._source_id is not None)

    def _on_unit_selected(self, _index: int) -> None:
        """Persist the chosen display unit for the current source and refresh the
        live measurement so SI + the new unit show side by side."""
        if self._project is None or self._source_id is None:
            return
        profile_id = self._unit_combo.currentData()
        self._project.set_source_unit_profile(self._source_id, profile_id)
        self._update_measure_readout()

    def manage_unit_profiles(self) -> None:
        """Open the create/edit/delete dialog for local unit profiles."""
        if self._project is None:
            QMessageBox.information(
                self, "No project",
                "Open or create a project first — unit profiles are saved per project.")
            return
        dlg = UnitProfilesDialog(self._project, self)
        dlg.exec()
        # A profile may have been added/renamed/deleted (deleting the active one
        # clears it on the source), so rebuild the combo and re-measure.
        self._refresh_unit_combo()
        self._update_measure_readout()

    def _active_unit_profile(self) -> dict | None:
        if self._project is None or self._source_id is None:
            return None
        return self._project.get_source_unit_profile(self._source_id)

    # -- identification templates & fields (Milestone 10) -------------------

    def manage_templates(self) -> None:
        """Open the create/edit/delete dialog for land-type templates."""
        if self._project is None:
            QMessageBox.information(
                self, "No project",
                "Open or create a project first — templates are saved per project.")
            return
        TemplatesDialog(self._project, self).exec()

    def edit_identification(self) -> None:
        """Open the identification-fields form for the active parcel."""
        if self._project is None or self._active_parcel_id is None:
            QMessageBox.information(
                self, "No parcel",
                "Select a parcel first — identification fields are per parcel.")
            return
        dlg = IdentificationDialog(self._project, self._active_parcel_id, self)
        if dlg.exec():
            # Owner may have changed in the form; keep the dock + list in sync.
            index = self._parcel_index(self._active_parcel_id)
            parcel = self._project.get_parcel(self._active_parcel_id)
            if index is not None and parcel is not None:
                self._parcels[index]["owner"] = parcel.get("owner")
                self._update_parcel_list_row(index)
            self._owner_edit.blockSignals(True)
            self._owner_edit.setText((parcel.get("owner") or "") if parcel else "")
            self._owner_edit.blockSignals(False)

    # -- readouts -----------------------------------------------------------

    def _update_scale_readout(self) -> None:
        if self._scale is None:
            self._scale_readout.setText("Scale: not set")
        else:
            s = self._scale
            self._scale_readout.setText(
                f"Scale: 1 px = {s.metres_per_pixel:.4g} m   (1 m = {s.pixels_per_metre:.4g} px)")

    def _update_measure_readout(self) -> None:
        pts = self.canvas.polygon_points()
        if not pts:
            self._measure_readout.setText("No boundary")
            return
        mpp = self._scale.metres_per_pixel if self._scale is not None else None
        m = measure_polygon(pts, mpp, closed=self.canvas.is_polygon_closed())
        n = m.point_count
        if m.has_scale:
            seg = f"{_fmt(m.last_segment_m)} m" if m.last_segment_m is not None else "—"
            perim = f"{_fmt(m.perimeter_m)} m"
            if n >= 3:
                # SI is always the baseline (Scale-first rule); a selected local
                # profile is shown alongside it, not instead of it. The profile is
                # areal, so only area gets the second unit — length stays SI.
                area = f"{_fmt(m.area_sq_m)} m²"
                prof = self._active_unit_profile()
                if prof is not None and prof["name"] != units.SI_AREA_UNIT:
                    area += f" = {_fmt(units.area_in_unit(m.area_sq_m, prof['sq_m_per_unit']))} {prof['name']}"
            else:
                area = "—"
            self._measure_readout.setText(
                f"Pts {n} | last seg {seg} | perim {perim} | area {area}")
        else:
            seg = f"{m.last_segment_px:.1f} px" if m.last_segment_px is not None else "—"
            perim = f"{m.perimeter_px:.1f} px"
            area = f"{m.area_px:.1f} px²" if n >= 3 else "—"
            self._measure_readout.setText(
                f"Pts {n} | last seg {seg} | perim {perim} | area {area}  (no scale)")

    def _update_title(self) -> None:
        parts = ["Land Measure Tool"]
        if self._project is not None:
            parts.append(self._project.get_meta("project_name") or "project")
        if self._current_path is not None:
            parts.append(Path(self._current_path).name)
        self.setWindowTitle(" — ".join(parts))

    # -- lifecycle ----------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self._project is not None:
            self._project.close()
            self._project = None
        super().closeEvent(event)


def _fmt(value: float) -> str:
    """Format a measurement value with ~4 significant figures, but without
    scientific notation for readable magnitudes (so an SI baseline like 10000 m²
    reads as '10000', not '1e+04'). Large numbers become plain integers; small
    ones keep their significant digits."""
    formatted = f"{value:.4g}"
    if "e" not in formatted and "E" not in formatted:
        return formatted
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _scale_from_db_row(row: dict) -> TwoPointScale:
    """Rebuild a TwoPointScale for display/measurement from a source_scales row.
    Our GUI always stores the two points and the distance, so this reproduces the
    original calibration; if some fields are absent, metres_per_pixel still
    drives the measurements correctly."""
    mpp = row["metres_per_pixel"]
    p1 = (row.get("p1x"), row.get("p1y"))
    p2 = (row.get("p2x"), row.get("p2y"))
    dist = row.get("real_distance_m")
    if None in p1 or None in p2:
        p1, p2 = (0.0, 0.0), (0.0, 0.0)
    pixel_distance = (dist / mpp) if (dist and mpp) else 0.0
    return TwoPointScale(
        p1=(float(p1[0]), float(p1[1])), p2=(float(p2[0]), float(p2[1])),
        pixel_distance=pixel_distance, real_distance_m=float(dist or 0.0),
        metres_per_pixel=float(mpp), method=row.get("method") or "two-point",
    )


def run(file: str | None = None, argv=None) -> int:
    """Launch the GUI. Optionally open *file* on startup. Returns the Qt exit code."""
    app = QApplication.instance() or QApplication(argv or [])
    win = MainWindow()
    if file:
        win.load_path(file)
    win.show()
    return app.exec()
