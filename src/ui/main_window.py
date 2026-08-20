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

from PySide6.QtCore import Qt, QEvent, QPointF, QRect, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QDockWidget, QFileDialog, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPushButton, QSlider, QStyledItemDelegate,
    QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QToolBar, QVBoxLayout,
    QWidget,
)

from ..core.geometry import measure_polygon
from ..core.project_db import ProjectDB, ProjectError
from ..core.scale import (
    compute_two_point_scale, compare_scales, metadata_scale_from_page,
    dxf_header_scale, TwoPointScale, METHOD_TWO_POINT, METHOD_PDF_METADATA,
    METHOD_DXF_HEADER,
)
from ..core.location import (
    observation_from_points, target_from_field, format_description,
    cross_validate_positions, SOURCE_FIELD, SOURCE_SHEET,
)
from ..core.selection import parcel_at_point, parcels_in_rect
from ..core import units
from ..export import report as report_export
from ..export import segment_report as segment_export
from ..io.raster import open_raster
from ..io.preprocess import preprocess_raster
from .canvas_view import CanvasView
from .unit_profiles_dialog import UnitProfilesDialog
from .templates_dialog import TemplatesDialog
from .identification_dialog import IdentificationDialog
from .report_dialog import OwnerReportDialog as ReportDialog

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
    "Supported documents (*.pdf *.dxf *.png *.jpg *.jpeg *.bmp *.tif *.tiff);;"
    "PDF (*.pdf);;"
    "DXF (*.dxf);;"
    "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff);;"
    "All files (*)"
)

#: Per-item data role holding a parcel row's overlay-hidden flag, so the row
#: delegate can paint the eye in the right state (Milestone 13).
_HIDDEN_ROLE = Qt.ItemDataRole.UserRole + 1


class _ParcelRowDelegate(QStyledItemDelegate):
    """Parcel-list row painter that adds a small, always-visible **eye toggle** at
    the right of each row and turns a click on it into an overlay-visibility
    toggle. This is the M13 discoverability fix (same lesson as M7's Select mode:
    a correct feature is useless if unfindable) — a one-click affordance that sits
    *alongside* the M7 selection checkbox and the right-click menu, replacing
    neither. Everything else about the row (checkbox, label, colour, selection) is
    the base delegate's and is left untouched. The eye is drawn from primitives so
    it renders identically without depending on an emoji font."""

    #: Emitted with the row index whose eye was clicked.
    eyeClicked = Signal(int)

    def _eye_size(self, rect: QRect) -> int:
        return min(rect.height() - 4, 20)

    def _eye_rect(self, rect: QRect) -> QRect:
        size = self._eye_size(rect)
        top = rect.top() + (rect.height() - size) // 2
        return QRect(rect.right() - size - 4, top, size, size)

    def paint(self, painter: QPainter, option, index) -> None:
        # Reserve room on the right so a long label never runs under the eye.
        opt = QStyleOptionViewItem(option)
        opt.rect = QRect(option.rect)
        opt.rect.setRight(option.rect.right() - self._eye_size(option.rect) - 10)
        super().paint(painter, opt, index)

        hidden = bool(index.data(_HIDDEN_ROLE))
        eye = self._eye_rect(option.rect)
        cx, cy = eye.center().x() + 1, eye.center().y() + 1
        rx, ry = eye.width() * 0.44, eye.height() * 0.30
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(120, 120, 120) if hidden else QColor(40, 40, 40))
        pen.setWidthF(1.4)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), rx, ry)      # eye outline (almond)
        if hidden:
            # A slash across the eye = 'overlay hidden'.
            painter.drawLine(int(cx - rx), int(cy + ry), int(cx + rx), int(cy - ry))
        else:
            painter.setBrush(QColor(40, 40, 40))
            painter.drawEllipse(QPointF(cx, cy), ry * 0.62, ry * 0.62)  # pupil
        painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        et = event.type()
        if et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                  QEvent.Type.MouseButtonDblClick):
            if self._eye_rect(option.rect).contains(event.position().toPoint()):
                # Consume every eye-area mouse event so the click toggles overlay
                # visibility *without* also changing the active/selected parcel.
                if et == QEvent.Type.MouseButtonRelease:
                    self.eyeClicked.emit(index.row())
                return True
        return super().editorEvent(event, model, option, index)


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
        self.canvas.segmentSelectionChanged.connect(self._on_segment_selection_changed)
        self.canvas.edgeHovered.connect(self._on_edge_hovered)
        self.canvas.locationClicked.connect(self._on_location_clicked)
        self.canvas.duplicatePointWarning.connect(self._on_duplicate_point_warning)
        self.canvas.assistRequested.connect(self._on_assist_requested)

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

        # Boundary-segment report (Milestone 12): the live side table's contents
        # for the active parcel. `_segment_edges` is every candidate edge (from the
        # export module, so the numbering matches); `_segment_neighbours` holds the
        # user's per-edge neighbouring-feature text keyed by edge index — this table
        # *is* the report content, not a separate model.
        self._segment_edges: list = []
        self._segment_context: dict = {}
        self._segment_neighbours: dict[int, str] = {}

        # Location-fixing (Milestone 16): the parcel the fixes attach to and the
        # in-progress click flow. `_loc_awaiting` is None, "reference", or "target";
        # `_loc_pending` holds a just-placed reference (px, label) while its
        # distance/bearing or target is captured.
        self._location_parcel_id: int | None = None
        self._loc_awaiting: str | None = None
        self._loc_pending: tuple | None = None
        self._loc_last_px: tuple | None = None   # previous location click, for M17 dup check

        # Image preprocessing (M8): a display-time, non-destructive preview. The
        # raw raster and the source file are never modified; the enhanced raster
        # is computed on demand and cached. Pixel coordinates are unchanged, so
        # scale/tracing/snapping work identically whichever is shown.
        self._raw_raster = None
        self._pre_raster = None       # cached preprocessed raster (lazily built)
        self._preprocess_on = False
        # DXF render metadata (Milestone 15): units + drawing-units-per-pixel of the
        # current source, kept so the header-scale candidate can be offered. None
        # for non-DXF sources.
        self._dxf_info = None

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
        self._build_review_toolbar()
        self._build_parcel_dock()
        self._build_segment_dock()
        self._build_location_dock()

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

        # PDF-metadata scale (Milestone 14): a second candidate, PDF sources only.
        self._pdf_scale_action = QAction("PDF scale", self)
        self._pdf_scale_action.setToolTip(
            "Derive a scale from this PDF page's physical size, and cross-check it "
            "against the manual scale. PDF sources only; offered, never auto-applied.")
        self._pdf_scale_action.triggered.connect(self.propose_pdf_metadata_scale)
        self._pdf_scale_action.setEnabled(False)   # enabled once a PDF is loaded
        bar.addAction(self._pdf_scale_action)

        # DXF-header scale (Milestone 15): a second candidate for DXF sources.
        self._dxf_scale_action = QAction("DXF scale", self)
        self._dxf_scale_action.setToolTip(
            "Derive a scale from this DXF's header units ($INSUNITS), and cross-check "
            "it against the manual scale. DXF sources only; offered, never auto-applied.")
        self._dxf_scale_action.triggered.connect(self.propose_dxf_header_scale)
        self._dxf_scale_action.setEnabled(False)   # enabled once a DXF is loaded
        bar.addAction(self._dxf_scale_action)

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

        # Location-fixing (Milestone 16): its own mutually-exclusive canvas mode.
        self._location_action = QAction("Locate", self, checkable=True)
        self._location_action.setToolTip(
            "Location-fix (encroachment): mark durable landmarks and a target point "
            "for a selected parcel to get distance + bearing. Needs a scale set.")
        self._location_action.toggled.connect(self._on_location_toggled)
        bar.addAction(self._location_action)

        # Semi-automated tracing assist (Milestone 18): its own mutually-exclusive
        # canvas mode. The action itself is created in _build_menu (which runs
        # first); here it just gets a toolbar button. Disabled for DXF.
        bar.addAction(self._assist_action)

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

    def _build_review_toolbar(self) -> None:
        """Visual confidence overlay (Milestone 13): a review row for checking a
        trace against the scan underneath — a global show/hide of every overlay and
        an opacity fade. (Per-parcel hiding lives on the Parcels list: right-click a
        parcel.) Everything here is display-only; it never changes geometry, the
        active parcel, the selection, or the current tool/mode."""
        bar = QToolBar("Review", self)
        bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)

        self._overlays_action = QAction("Overlays", self, checkable=True)
        self._overlays_action.setChecked(True)
        self._overlays_action.setToolTip(
            "Show/hide ALL overlays to compare the trace against the raw scan. "
            "A display toggle only — it does not leave the current tool or lose work.")
        self._overlays_action.toggled.connect(self._on_overlays_toggled)
        bar.addAction(self._overlays_action)

        bar.addSeparator()
        bar.addWidget(QLabel(" Overlay opacity: "))
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setMinimum(10)      # never fully invisible via the slider
        self._opacity_slider.setMaximum(100)
        self._opacity_slider.setValue(100)
        self._opacity_slider.setFixedWidth(120)
        self._opacity_slider.setToolTip(
            "Fade the traced boundaries against the scan to spot subtle "
            "misalignment. Purely visual — geometry is unchanged.")
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        bar.addWidget(self._opacity_slider)
        self._opacity_label = QLabel("100%")
        bar.addWidget(self._opacity_label)

        bar.addSeparator()
        self._show_all_parcels_act = QAction("Show all parcels", self)
        self._show_all_parcels_act.setToolTip(
            "Un-hide every parcel overlay hidden via the Parcels list.")
        self._show_all_parcels_act.triggered.connect(self.show_all_parcel_overlays)
        bar.addAction(self._show_all_parcels_act)

        # Mirror the review controls in a menu for discoverability (built here,
        # after the actions exist).
        review_menu = self.menuBar().addMenu("Re&view")
        review_menu.addAction(self._overlays_action)
        review_menu.addAction(self._show_all_parcels_act)

    def _on_overlays_toggled(self, checked: bool) -> None:
        # Display-only: never changes the active tool/mode or any stored data.
        self.canvas.set_overlays_visible(checked)
        self._status.setText(
            "Overlays shown." if checked
            else "Overlays hidden — showing the raw scan for comparison.")

    def _on_opacity_changed(self, value: int) -> None:
        self._opacity_label.setText(f"{value}%")
        self.canvas.set_overlay_opacity(value / 100.0)

    def _reset_review_controls(self) -> None:
        """Sync the review toolbar back to defaults (all shown, full opacity) —
        used when a new source is loaded, whose canvas overlay state also resets."""
        self._overlays_action.blockSignals(True)
        self._overlays_action.setChecked(True)
        self._overlays_action.blockSignals(False)
        self._opacity_slider.blockSignals(True)
        self._opacity_slider.setValue(100)
        self._opacity_slider.blockSignals(False)
        self._opacity_label.setText("100%")

    def show_all_parcel_overlays(self) -> None:
        """Clear every per-parcel overlay hide (Milestone 13)."""
        self.canvas.set_hidden_parcel_ids([])
        self._refresh_parcel_visibility_cues()

    def toggle_parcel_overlay(self, parcel_id) -> None:
        """Hide/show one parcel's overlay for review (Milestone 13). Independent of
        which parcel is active or selected — a pure display state on the canvas."""
        now_hidden = not self.canvas.is_parcel_hidden(parcel_id)
        self.canvas.set_parcel_hidden(parcel_id, now_hidden)
        self._refresh_parcel_visibility_cues()

    def _on_parcel_eye_clicked(self, row: int) -> None:
        """A click on a row's eye indicator toggles that parcel's overlay — the
        exact same action as the right-click 'Hide/Show overlay', just a visible
        one-click path. Both refresh through the same code, so the eye glyph, the
        '(overlay hidden)' cue, and the canvas stay in lock-step."""
        if 0 <= row < len(self._parcels):
            self.toggle_parcel_overlay(self._parcels[row]["id"])

    def _on_parcel_context_menu(self, pos) -> None:
        item = self._parcel_list.itemAt(pos)
        if item is None:
            return
        pid = item.data(Qt.ItemDataRole.UserRole)
        hidden = self.canvas.is_parcel_hidden(pid)
        menu = QMenu(self._parcel_list)
        toggle = menu.addAction("Show overlay" if hidden else "Hide overlay")
        chosen = menu.exec(self._parcel_list.viewport().mapToGlobal(pos))
        if chosen is toggle:
            self.toggle_parcel_overlay(pid)

    def _apply_row_visibility_cue(self, row: int) -> None:
        """Reflect one parcel's overlay-hidden state on its row: a dimmed,
        '(overlay hidden)'-suffixed label plus the ``_HIDDEN_ROLE`` flag the eye
        delegate paints from. Shared by every row-refresh path so the eye icon and
        the text cue can never disagree. Caller blocks list signals."""
        if not (0 <= row < len(self._parcels)):
            return
        item = self._parcel_list.item(row)
        if item is None:
            return
        pid = item.data(Qt.ItemDataRole.UserRole)
        hidden = self.canvas.is_parcel_hidden(pid)
        item.setText(self._parcel_label(row, self._parcels[row])
                     + ("  (overlay hidden)" if hidden else ""))
        color = QColor(_parcel_color(row))
        if hidden:
            color.setAlpha(90)
        item.setForeground(color)
        item.setData(_HIDDEN_ROLE, hidden)

    def _refresh_parcel_visibility_cues(self) -> None:
        """Re-apply the overlay-hidden cue (label + eye) to every row, without
        disturbing the active row."""
        self._parcel_list.blockSignals(True)
        for row in range(self._parcel_list.count()):
            self._apply_row_visibility_cue(row)
        self._parcel_list.blockSignals(False)
        self._parcel_list.viewport().update()   # repaint the eye indicators

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
        # Per-row eye toggle (Milestone 13): a visible one-click affordance for
        # overlay visibility, drawn by the delegate and clicked directly.
        self._parcel_delegate = _ParcelRowDelegate(self._parcel_list)
        self._parcel_delegate.eyeClicked.connect(self._on_parcel_eye_clicked)
        self._parcel_list.setItemDelegate(self._parcel_delegate)
        # Right-click a parcel to hide/show its overlay too (secondary path).
        self._parcel_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._parcel_list.customContextMenuRequested.connect(self._on_parcel_context_menu)
        self._parcel_list.setToolTip(
            "Click a parcel to edit it; tick its box to add it to the selection; "
            "click the eye (or right-click) to hide/show just its overlay (review).")
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

    # -- boundary-segment report dock (Milestone 12) ------------------------

    _SEG_COLS = ("Seq", "From", "To", "Length", "Bearing", "Neighbour")
    _SEG_NEIGHBOUR_COL = 5

    def _build_segment_dock(self) -> None:
        """Right-side dock (same pattern as Parcels) that fills live as boundary
        edges are picked on the canvas: one row per selected segment plus a running
        total. Editing the Neighbour cell here is what feeds the exported
        'North: bounded by [X], 45 m' line — same data, no separate model."""
        dock = QDockWidget("Boundary segments", self)
        dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea |
                             Qt.DockWidgetArea.RightDockWidgetArea)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(6, 6, 6, 6)

        self._segment_hint = QLabel(
            "Pick a parcel, then ‘Describe boundary’ — click edges on the canvas "
            "to build a contiguous run.")
        self._segment_hint.setWordWrap(True)
        layout.addWidget(self._segment_hint)

        self._segment_table = QTableWidget(0, len(self._SEG_COLS))
        self._segment_table.setHorizontalHeaderLabels(self._SEG_COLS)
        self._segment_table.verticalHeader().setVisible(False)
        self._segment_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked |
            QAbstractItemView.EditTrigger.SelectedClicked)
        self._segment_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._segment_table.horizontalHeader().setSectionResizeMode(
            self._SEG_NEIGHBOUR_COL, QHeaderView.ResizeMode.Stretch)
        self._segment_table.itemChanged.connect(self._on_segment_neighbour_edited)
        layout.addWidget(self._segment_table, 1)

        self._segment_total_label = QLabel("Total selected: —")
        layout.addWidget(self._segment_total_label)

        buttons = QHBoxLayout()
        self._segment_clear_btn = QPushButton("Clear")
        self._segment_clear_btn.clicked.connect(self._clear_segment_selection)
        self._segment_generate_btn = QPushButton("Generate report…")
        self._segment_generate_btn.clicked.connect(self.generate_segment_report)
        buttons.addWidget(self._segment_clear_btn)
        buttons.addWidget(self._segment_generate_btn)
        layout.addLayout(buttons)

        dock.setWidget(body)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._segment_dock = dock
        self._segment_generate_btn.setEnabled(False)
        self._segment_clear_btn.setEnabled(False)

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

        reports_menu = self.menuBar().addMenu("Rep&orts")
        owner_report_act = QAction("&Owner-wise summary…", self)
        owner_report_act.setToolTip(
            "Export every parcel grouped by owner, with per-owner area totals "
            "(PDF / CSV / JSON)")
        owner_report_act.triggered.connect(self.export_owner_report)
        reports_menu.addAction(owner_report_act)
        boundary_report_act = QAction("&Boundary description (segments)…", self)
        boundary_report_act.setToolTip(
            "Describe a parcel's boundary edge-by-edge: pick a contiguous run of "
            "edges on the canvas, then export (PDF / CSV / JSON)")
        boundary_report_act.triggered.connect(self.begin_segment_description)
        reports_menu.addAction(boundary_report_act)

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
        poly_menu.addSeparator()
        check_corners_act = QAction("Check for &missed corners", self)
        check_corners_act.setToolTip(
            "Flag traced edges that look long/straight but don't track the scan — a "
            "possible skipped corner. A warning only; nothing is changed.")
        check_corners_act.triggered.connect(self.check_missing_corners)
        poly_menu.addAction(check_corners_act)
        poly_menu.addSeparator()
        # Line-follow assist (M18): created here (menu is built before the toolbar,
        # which also shows it). Disabled for DXF via _sync_assist_action.
        self._assist_action = QAction("&Assist trace (follow line)", self, checkable=True)
        self._assist_action.setToolTip(
            "Follow a printed boundary line between two clicks (magnetic-lasso style). "
            "The path is shown for you to accept (Enter) or reject (Esc) — never "
            "auto-added. Manual tracing stays available. Not for DXF sources.")
        self._assist_action.toggled.connect(self._on_assist_toggled)
        poly_menu.addAction(self._assist_action)
        accept_assist_act = QAction("&Accept followed path", self)
        accept_assist_act.setToolTip("Accept the previewed followed path (or press Enter)")
        accept_assist_act.triggered.connect(self.accept_assist)
        poly_menu.addAction(accept_assist_act)
        reject_assist_act = QAction("&Reject followed path", self)
        reject_assist_act.setToolTip("Reject the previewed path (or press Esc); boundary unchanged")
        reject_assist_act.triggered.connect(self.reject_assist)
        poly_menu.addAction(reject_assist_act)
        poly_menu.addSeparator()
        self._segment_action = QAction("&Describe boundary (select segments)", self,
                                       checkable=True)
        self._segment_action.setToolTip(
            "Click boundary edges of the active parcel to build a contiguous run, "
            "then generate a boundary-description report")
        self._segment_action.toggled.connect(self._on_segment_toggled)
        poly_menu.addAction(self._segment_action)

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
        self._refresh_parcel_visibility_cues()   # reflect any per-parcel overlay hides

    def _parcel_label(self, index: int, parcel: dict) -> str:
        owner = parcel.get("owner") or "(no owner)"
        n = parcel.get("point_count", 0)
        mark = " ✓" if parcel.get("closed") else ""
        return f"{index + 1}. {owner} — {n} pt{'s' if n != 1 else ''}{mark}"

    def _set_active_parcel(self, parcel_id: int | None) -> None:
        self._active_parcel_id = parcel_id
        # Let the canvas know which parcel owns the editable boundary, so a
        # per-parcel overlay hide (M13) can apply to it too — set before the
        # polygon is (re)drawn below.
        self.canvas.set_active_parcel_id(parcel_id)
        if parcel_id is None:
            self.canvas.set_polygon([], closed=False)
            self.canvas.set_background_polygons([])
            self._refresh_snap_vertices()
            self._owner_edit.blockSignals(True)
            self._owner_edit.setText("")
            self._owner_edit.blockSignals(False)
            if self._segment_action.isChecked():   # nothing to describe now
                self._set_segment_checked(False)
                self.canvas.stop_segment_selection()
                self._segment_edges = []
                self._refresh_segment_table()
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
        # In segment mode, re-point the side table at the new parcel's edges (the
        # canvas already cleared the now-invalid selection in set_polygon).
        if self._segment_action.isChecked():
            try:
                self._load_segment_edges()
            except segment_export.SegmentReportError:
                self._segment_edges, self._segment_context = [], {}
            self._refresh_segment_table()
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
            self._set_segment_checked(False)
            self._set_location_checked(False)
            self._set_assist_checked(False)
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
        # Route through the shared cue helper so an owner/point-count refresh keeps
        # the '(overlay hidden)' suffix and eye state intact.
        self._parcel_list.blockSignals(True)
        self._apply_row_visibility_cue(index)
        self._parcel_list.blockSignals(False)

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
        dxf_info = None
        try:
            if Path(path).suffix.lower() == ".dxf":
                # Render via the DXF loader directly so its header/scale metadata is
                # captured; the resulting raster is the same neutral contract as any
                # other source, so everything downstream is unchanged.
                from ..io.dxf_loader import render_dxf
                dxf_info = render_dxf(path)
                raster = dxf_info.raster
            else:
                raster = open_raster(path)
        except Exception as exc:  # decoding errors are user-facing, not crashes
            QMessageBox.critical(self, "Could not open file", f"{Path(path).name}\n\n{exc}")
            self._status.setText("Open a PDF or image to begin.")
            return
        self.canvas.set_image(raster)   # clears any prior markers/boundary + exits selection mode
        self._set_select_checked(False)
        self._reset_review_controls()   # canvas reset its overlay state; mirror it in the UI
        self._raw_raster = raster
        self._dxf_info = dxf_info
        self._pre_raster = None         # invalidate any cached enhancement
        self._current_path = str(path)
        self._source_id = None
        self._scale = None              # a new file has its own scale/boundaries
        self._parcels = []
        self._active_parcel_id = None
        self._selected_parcel_ids.clear()   # selection is per-source; start fresh
        self._apply_display_image()     # honour the preprocessing toggle for the new file
        self._sync_pdf_scale_action()   # PDF-metadata scale is offered for PDFs only
        self._sync_dxf_scale_action()   # DXF-header scale is offered for DXFs only
        self._sync_assist_action()      # line-follow assist is disabled for DXF
        self._attach_source_to_project()
        self._update_scale_readout()
        self._update_measure_readout()
        msg = f"{Path(path).name}   —   {raster.width} × {raster.height} px"
        if dxf_info is not None:
            if dxf_info.skipped_entity_types:
                # Flag, never silently drop, entity types we don't render (M15 scope).
                msg += f"   (DXF: not drawn — {', '.join(dxf_info.skipped_entity_types)})"
            if dxf_info.precision_limited:
                # Be honest when a large drawing can't be rendered finely enough for
                # precise tracing, rather than letting the user discover it later.
                msg += (f"   (precision limited: ~{dxf_info.metres_per_pixel:.2f} m/px, "
                        f"coarser than the {dxf_info.precision_target_m:g} m/px target — "
                        "large drawing; trace critical corners with care)")
        self._status.setText(msg)
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
        self._set_segment_checked(False)
        self._set_location_checked(False)
        self._set_assist_checked(False)
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

    # -- PDF-metadata scale (Milestone 14) ----------------------------------

    def _current_is_pdf(self) -> bool:
        return (self._current_path is not None
                and Path(self._current_path).suffix.lower() == ".pdf")

    def _sync_pdf_scale_action(self) -> None:
        """Enable the PDF-metadata scale action only for PDF sources (images carry
        no reliable scale metadata, per the brief's file-format scope)."""
        self._pdf_scale_action.setEnabled(self._current_is_pdf() and self._raw_raster is not None)

    def _manual_scale(self) -> TwoPointScale | None:
        """The current scale iff it came from manual M3 calibration — the baseline
        the metadata candidate is cross-checked against."""
        if self._scale is not None and self._scale.method == METHOD_TWO_POINT:
            return self._scale
        return None

    def _compute_metadata_scale(self):
        """Read this PDF page's physical size and derive a scale candidate, or show
        why it can't and return None. Never applies anything."""
        try:
            from ..io.pdf_loader import read_page_size_points
            w_pt, h_pt = read_page_size_points(self._current_path, page=0)
            return metadata_scale_from_page(w_pt, h_pt,
                                            self._raw_raster.width, self._raw_raster.height)
        except Exception as exc:   # implausible/missing metadata, decode error, ...
            QMessageBox.information(
                self, "No usable PDF scale metadata",
                "Could not derive a scale from this PDF's page size:\n\n"
                f"{exc}\n\nUse the manual two-point scale instead.")
            return None

    def propose_pdf_metadata_scale(self) -> None:
        """Offer a PDF-metadata-derived scale (method 1). If a manual scale already
        exists, show both and their agreement/disagreement (method 4) rather than
        silently choosing. Nothing is applied without an explicit confirmation."""
        if not self._current_is_pdf() or self._raw_raster is None:
            QMessageBox.information(
                self, "Not a PDF",
                "PDF metadata scale is only available for PDF sources — images have "
                "no reliable embedded scale.")
            return
        meta = self._compute_metadata_scale()
        if meta is None:
            return

        manual = self._manual_scale()
        meta_line = (f"PDF page {meta.width_pt:.0f} × {meta.height_pt:.0f} pt "
                     f"({meta.width_m:.3f} × {meta.height_m:.3f} m)\n"
                     f"→ PDF-metadata scale: 1 px = {meta.metres_per_pixel:.4g} m")
        if manual is not None:
            cc = compare_scales(manual.metres_per_pixel, meta.metres_per_pixel)
            body = (f"Manual scale: 1 px = {manual.metres_per_pixel:.4g} m\n"
                    f"{meta_line}\n\n{cc.describe()}\n\n"
                    "Replace the manual scale with the PDF-metadata scale?")
            if QMessageBox.question(self, "Two scales — cross-check", body) \
                    == QMessageBox.StandardButton.Yes:
                self._apply_metadata_scale(meta)
            else:
                self._status.setText("Kept manual scale. " + cc.describe())
        else:
            body = (f"{meta_line}\n\n"
                    "Valid only if the page was generated at true (1:1) physical "
                    "scale. Use it as this source's scale?\n"
                    "(You can still set a manual scale instead.)")
            if QMessageBox.question(self, "Scale from PDF metadata", body) \
                    == QMessageBox.StandardButton.Yes:
                self._apply_metadata_scale(meta)
            else:
                self._status.setText("PDF-metadata scale not applied.")

    def _apply_metadata_scale(self, meta) -> None:
        """Adopt a PDF-metadata scale candidate (only from an explicit confirmation
        in :meth:`propose_pdf_metadata_scale`)."""
        self._apply_candidate_scale(
            meta.metres_per_pixel, METHOD_PDF_METADATA, "PDF metadata",
            note=f"PDF page {meta.width_pt:.0f}x{meta.height_pt:.0f} pt "
                 f"({meta.width_m:.3f}x{meta.height_m:.3f} m)")

    def _apply_candidate_scale(self, metres_per_pixel: float, method: str,
                               label: str, *, note: str) -> None:
        """Adopt a derived (non-manual) scale candidate as the source's scale.
        Shared by the PDF-metadata (M14) and DXF-header (M15) offers — both only
        ever reach here on an explicit user confirmation."""
        self._scale = TwoPointScale(
            p1=(0.0, 0.0), p2=(0.0, 0.0), pixel_distance=0.0, real_distance_m=0.0,
            metres_per_pixel=metres_per_pixel, method=method)
        if self._project is not None and self._source_id is not None:
            self._project.set_source_scale(
                self._source_id, metres_per_pixel, method=method, note=note)
            self._project.refresh_source_vertices_si(self._source_id, metres_per_pixel)
        self._update_scale_readout()
        self._update_measure_readout()
        self._status.setText(f"Scale set from {label}: 1 px = {metres_per_pixel:.4g} m.")

    # -- DXF-header scale (Milestone 15) ------------------------------------

    def _current_is_dxf(self) -> bool:
        return (self._current_path is not None
                and Path(self._current_path).suffix.lower() == ".dxf")

    def _sync_dxf_scale_action(self) -> None:
        """Enable the DXF-header scale action only for DXF sources."""
        self._dxf_scale_action.setEnabled(self._current_is_dxf() and self._dxf_info is not None)

    def propose_dxf_header_scale(self) -> None:
        """Offer a DXF-header-derived scale (method 1 for DXF). Like the PDF offer:
        cross-checked against a manual scale when one exists, applied only on an
        explicit confirmation, never silently."""
        if not self._current_is_dxf() or self._dxf_info is None:
            QMessageBox.information(
                self, "Not a DXF",
                "DXF-header scale is only available for DXF sources.")
            return
        try:
            cand = dxf_header_scale(self._dxf_info.insunits, self._dxf_info.units_per_pixel)
        except ValueError as exc:   # unitless / unknown $INSUNITS
            QMessageBox.information(
                self, "No usable DXF units",
                f"This DXF's header carries no real-world unit:\n\n{exc}\n\n"
                "Use the manual two-point scale instead.")
            return

        manual = self._manual_scale()
        cand_line = (f"DXF header unit: {cand.unit_name} ({cand.metres_per_unit:g} m/unit)\n"
                     f"→ DXF-header scale: 1 px = {cand.metres_per_pixel:.4g} m")
        if manual is not None:
            cc = compare_scales(manual.metres_per_pixel, cand.metres_per_pixel)
            body = (f"Manual scale: 1 px = {manual.metres_per_pixel:.4g} m\n"
                    f"{cand_line}\n\n{cc.describe()}\n\n"
                    "Replace the manual scale with the DXF-header scale?")
            if QMessageBox.question(self, "Two scales — cross-check", body) \
                    == QMessageBox.StandardButton.Yes:
                self._apply_dxf_scale(cand)
            else:
                self._status.setText("Kept manual scale. " + cc.describe())
        else:
            body = (f"{cand_line}\n\nUse it as this source's scale?\n"
                    "(You can still set a manual scale instead.)")
            if QMessageBox.question(self, "Scale from DXF header", body) \
                    == QMessageBox.StandardButton.Yes:
                self._apply_dxf_scale(cand)
            else:
                self._status.setText("DXF-header scale not applied.")

    def _apply_dxf_scale(self, cand) -> None:
        self._apply_candidate_scale(
            cand.metres_per_pixel, METHOD_DXF_HEADER, "DXF header",
            note=f"DXF $INSUNITS={cand.insunits} ({cand.unit_name}), "
                 f"{cand.units_per_pixel:.6g} unit/px")

    # -- location-fixing (Milestone 16) -------------------------------------

    _LOC_COLS = ("Landmark", "Distance", "Bearing", "Source")

    def _build_location_dock(self) -> None:
        """Right-side dock listing a parcel's reference-landmark observations with a
        live cross-check of the positions they imply (a review aid; the fixes
        persist on the parcel for a future report)."""
        dock = QDockWidget("Location fixes", self)
        dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea |
                             Qt.DockWidgetArea.RightDockWidgetArea)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(6, 6, 6, 6)

        self._location_hint = QLabel(
            "‘Locate’ mode: click a durable landmark, then give a field "
            "distance/bearing to the parcel — or point to the target on the sheet. "
            "Bearings are screen-up = North (the sheet's top), not guaranteed true "
            "north if the scan is rotated.")
        self._location_hint.setWordWrap(True)
        layout.addWidget(self._location_hint)

        self._location_table = QTableWidget(0, len(self._LOC_COLS))
        self._location_table.setHorizontalHeaderLabels(self._LOC_COLS)
        self._location_table.verticalHeader().setVisible(False)
        self._location_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._location_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._location_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._location_table, 1)

        self._location_crosscheck = QLabel("Cross-check: —")
        self._location_crosscheck.setWordWrap(True)
        layout.addWidget(self._location_crosscheck)

        buttons = QHBoxLayout()
        self._loc_add_btn = QPushButton("Add landmark…")
        self._loc_add_btn.clicked.connect(self.add_location_landmark)
        self._loc_clear_btn = QPushButton("Clear fixes")
        self._loc_clear_btn.clicked.connect(self.clear_location_fixes)
        buttons.addWidget(self._loc_add_btn)
        buttons.addWidget(self._loc_clear_btn)
        layout.addLayout(buttons)

        dock.setWidget(body)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._location_dock = dock

    def _set_location_checked(self, on: bool) -> None:
        self._location_action.blockSignals(True)
        self._location_action.setChecked(on)
        self._location_action.blockSignals(False)

    def _location_target_parcel(self) -> int | None:
        """The parcel a fix attaches to, scoped by the M7 selection: the active
        parcel if it's in the selection, else the first selected parcel, else the
        active parcel. None if there are no parcels at all."""
        selected = self.selected_parcel_ids()
        if selected:
            if self._active_parcel_id in selected:
                return self._active_parcel_id
            return selected[0]
        return self._active_parcel_id

    def begin_location_fix(self) -> None:
        """Menu/programmatic entry: latch the checkable toggle."""
        if self._location_action.isChecked():
            self._on_location_toggled(True)
        else:
            self._location_action.setChecked(True)

    def _on_location_toggled(self, checked: bool) -> None:
        if not checked:
            if self.canvas.is_locating():
                self.canvas.stop_location_fix()
            self._loc_awaiting = None
            self._loc_pending = None
            self._loc_last_px = None
            self._status.setText("Location-fix mode off.")
            return

        if self._project is None or self._source_id is None or not self.canvas.has_image():
            self._set_location_checked(False)
            QMessageBox.information(self, "No document",
                                   "Open a project and a source first.")
            return
        # Scale-first: the distance/bearing result is meaningless without a scale.
        if self._project.get_source_scale(self._source_id) is None:
            self._set_location_checked(False)
            QMessageBox.information(
                self, "No scale set",
                "Set a scale for this source first (Set scale / PDF scale / DXF "
                "scale) — a location fix reports real distances, which need a scale.")
            return
        target_pid = self._location_target_parcel()
        if target_pid is None:
            self._set_location_checked(False)
            QMessageBox.information(
                self, "No parcel",
                "Select (or create) a parcel first — a location fix attaches to a "
                "parcel. Use Select parcels, or New parcel.")
            return

        self._location_parcel_id = target_pid
        self._set_select_checked(False)
        self._set_segment_checked(False)
        self._set_assist_checked(False)
        if not self.canvas.start_location_fix():
            self._set_location_checked(False)
            return
        self._sync_crosshair_action()
        self._loc_awaiting = "reference"
        self._loc_pending = None
        self._loc_last_px = None
        self._refresh_location_ui()
        self._status.setText(
            "Location-fix: click a durable landmark (tubewell, building, …) on the "
            "sheet.")

    def add_location_landmark(self) -> None:
        """Start capturing another landmark (the dock button). Enters Locate mode if
        not already in it."""
        if not self.canvas.is_locating():
            self.begin_location_fix()
            return
        self._loc_awaiting = "reference"
        self._loc_pending = None
        self._status.setText("Click the landmark on the sheet.")

    def _on_location_clicked(self, scene_pt) -> None:
        if not self.canvas.is_locating():
            return
        px = (scene_pt.x(), scene_pt.y())
        # Guard rail (M17): flag a click implausibly close to the previous one (a
        # probable double-click) — warn, but still use it; never auto-discard.
        from ..io.guardrails import is_duplicate_point
        if self._loc_last_px is not None and is_duplicate_point(px, self._loc_last_px):
            self._status.setText(
                "⚠ This point is very close to the previous one — possible "
                "double-click. Re-place it if that was accidental.")
        self._loc_last_px = px
        if self._loc_awaiting == "target" and self._loc_pending is not None:
            ref_px, label = self._loc_pending
            target = self._snap_location_target(px)
            self.add_location_reference_sheet(ref_px, target, label)
            self._loc_pending = None
            self._loc_awaiting = "reference"
        else:
            self._capture_reference(px)

    def _capture_reference(self, ref_px) -> None:
        """A landmark was clicked: name it, then either take a field measurement or
        wait for the user to point at the target on the sheet (the 'both' design)."""
        n = len(self._project.list_location_fixes(self._location_parcel_id)) \
            if self._project and self._location_parcel_id else 0
        label, ok = QInputDialog.getText(self, "Landmark name", "Landmark (e.g. tubewell):",
                                         text=f"Ref {n + 1}")
        if not ok:
            self._status.setText("Landmark cancelled.")
            return
        label = label.strip() or f"Ref {n + 1}"
        has_field = QMessageBox.question(
            self, "Measurement",
            "Do you have a field-measured distance from this landmark to the parcel?\n\n"
            "Yes — enter the distance (and bearing, if known).\n"
            "No — point to the target boundary point on the sheet.") \
            == QMessageBox.StandardButton.Yes
        if has_field:
            distance, ok = QInputDialog.getDouble(
                self, "Field distance", "Distance to the parcel (metres):",
                10.0, 0.0, 1_000_000.0, 2)
            if not ok:
                self._status.setText("Landmark cancelled.")
                return
            bearing = None
            if QMessageBox.question(
                    self, "Bearing",
                    "Do you know the compass bearing (screen-up = North)?") \
                    == QMessageBox.StandardButton.Yes:
                bearing, ok = QInputDialog.getDouble(
                    self, "Field bearing", "Bearing in degrees (0 = North, clockwise):",
                    0.0, 0.0, 360.0, 1)
                if not ok:
                    bearing = None
            self.add_location_reference_field(ref_px, label, distance, bearing)
            self._loc_awaiting = "reference"
        else:
            self._loc_pending = (ref_px, label)
            self._loc_awaiting = "target"
            self._status.setText(f"Now click the target boundary point for “{label}”.")

    def add_location_reference_field(self, ref_px, label, distance_m,
                                     bearing_deg) -> int | None:
        """Persist a FIELD observation (user-supplied distance + optional bearing);
        the target position is computed by trigonometry when a bearing is given.
        Testable entry point (bypasses dialogs)."""
        mpp = self._mpp()
        if self._project is None or self._location_parcel_id is None or mpp is None:
            return None
        target = (target_from_field(ref_px, distance_m, bearing_deg, mpp)
                  if bearing_deg is not None else None)
        fid = self._project.add_location_fix(
            self._location_parcel_id, label, ref_px, distance_m,
            bearing_deg=bearing_deg, target=target, source=SOURCE_FIELD)
        self._refresh_location_ui()
        self._status.setText(format_description(label, distance_m, bearing_deg))
        return fid

    def add_location_reference_sheet(self, ref_px, target_px, label) -> int | None:
        """Persist a SHEET observation: distance + bearing computed from the marked
        reference and target using the source scale. Testable entry point."""
        mpp = self._mpp()
        if self._project is None or self._location_parcel_id is None or mpp is None:
            return None
        distance_m, bearing = observation_from_points(ref_px, target_px, mpp)
        fid = self._project.add_location_fix(
            self._location_parcel_id, label, ref_px, distance_m,
            bearing_deg=bearing, target=target_px, source=SOURCE_SHEET)
        self._refresh_location_ui()
        self._status.setText(format_description(label, distance_m, bearing))
        return fid

    def _snap_location_target(self, px):
        """Snap a clicked target to a nearby selected/target parcel vertex, so a
        location reuses an existing traced corner rather than a near-miss click."""
        if self._project is None:
            return px
        pids = self.selected_parcel_ids() or (
            [self._location_parcel_id] if self._location_parcel_id else [])
        best, best_d = None, 12.0    # scene-pixel snap tolerance
        for pid in pids:
            for vx, vy in self._project.get_parcel_polygon(pid):
                d = ((vx - px[0]) ** 2 + (vy - px[1]) ** 2) ** 0.5
                if d < best_d:
                    best, best_d = (vx, vy), d
        return best if best is not None else px

    def clear_location_fixes(self) -> None:
        if self._project is None or self._location_parcel_id is None:
            return
        if not self._project.list_location_fixes(self._location_parcel_id):
            return
        if QMessageBox.question(self, "Clear location fixes",
                                "Remove all location fixes for this parcel?") \
                == QMessageBox.StandardButton.Yes:
            self._project.clear_location_fixes(self._location_parcel_id)
            self._loc_pending = None
            self._refresh_location_ui()
            self._status.setText("Location fixes cleared.")

    def _location_fixes(self) -> list:
        if self._project is None or self._location_parcel_id is None:
            return []
        return self._project.list_location_fixes(self._location_parcel_id)

    def _refresh_location_ui(self) -> None:
        self._refresh_location_table()
        self._refresh_location_overlay()
        self._update_location_crosscheck()

    def _refresh_location_table(self) -> None:
        fixes = self._location_fixes()
        table = self._location_table
        table.setRowCount(len(fixes))
        for row, fx in enumerate(fixes):
            bearing = ("—" if fx["bearing_deg"] is None
                       else f"{fx['bearing_deg']:.0f} deg")
            values = [fx["label"], f"{fx['distance_m']:.1f} m", bearing, fx["source"]]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                table.setItem(row, col, item)

    def _refresh_location_overlay(self) -> None:
        markers, links = [], []
        for fx in self._location_fixes():
            markers.append((fx["ref_x"], fx["ref_y"], fx["label"], "ref"))
            if fx["target_x"] is not None and fx["target_y"] is not None:
                markers.append((fx["target_x"], fx["target_y"], "", "target"))
                links.append((fx["ref_x"], fx["ref_y"], fx["target_x"], fx["target_y"]))
        self.canvas.set_location_overlay(markers, links)

    def _update_location_crosscheck(self) -> None:
        points = [(fx["target_x"], fx["target_y"]) for fx in self._location_fixes()
                  if fx["target_x"] is not None and fx["target_y"] is not None]
        mpp = self._mpp()
        if not points or mpp is None:
            self._location_crosscheck.setText("Cross-check: —")
            return
        cc = cross_validate_positions(points, mpp)
        self._location_crosscheck.setText("Cross-check: " + cc.describe())

    # -- polygon tracing ----------------------------------------------------

    def begin_polygon_tracing(self) -> None:
        if not self.canvas.start_polygon():
            QMessageBox.information(self, "No document", "Open a PDF or image first.")
            return
        self._set_select_checked(False)   # canvas already left selection mode
        self._set_segment_checked(False)
        self._set_location_checked(False)
        self._set_assist_checked(False)
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
        # Guard rail (M17): quietly check the just-closed boundary for edges that
        # may skip a corner, highlighting them; no dialog, nothing auto-changed.
        self.check_missing_corners(announce=False)

    # -- point-data guard rails (Milestone 17) ------------------------------

    def _on_duplicate_point_warning(self, index: int) -> None:
        """A traced point landed implausibly close to the previous one — warn, but
        keep it (never auto-remove). The user decides via Undo pt."""
        self._status.setText(
            f"⚠ Point {index + 1} is very close to the previous point — possible "
            "double-click. Use 'Undo pt' if it was accidental.")

    def _source_is_dxf(self) -> bool:
        return self._dxf_info is not None

    def check_missing_corners(self, announce: bool = True) -> None:
        """Flag active-boundary edges whose straight chord doesn't track the scan
        (a possibly-skipped corner). Warning only — highlights edges and reports a
        count; never moves or removes a point. Skipped for DXF (vector geometry is
        exact, so there's no scan ambiguity to sample)."""
        pts = self.canvas.polygon_points()
        if len(pts) < 2 or self._raw_raster is None:
            self.canvas.set_edge_warnings([])
            if announce:
                QMessageBox.information(self, "Nothing to check",
                                       "Trace a boundary first.")
            return
        if self._source_is_dxf():
            self.canvas.set_edge_warnings([])
            if announce:
                QMessageBox.information(
                    self, "Skipped for DXF",
                    "The missed-corner check samples scan pixels, which is meaningless "
                    "for a DXF's exact vector geometry, so it is skipped for DXF sources.")
            else:
                self._status.setText("Missed-corner check skipped for DXF (vector geometry).")
            return

        from ..io.guardrails import find_missing_corner_edges
        warnings = find_missing_corner_edges(
            self._raw_raster, pts, self.canvas.is_polygon_closed())
        self.canvas.set_edge_warnings([w.edge_index for w in warnings])
        if warnings:
            msg = (f"⚠ {len(warnings)} edge(s) look long/straight but don't track "
                   "the scan — a corner may have been missed. Review the highlighted "
                   "edges (in red); add points where needed, or ignore if fine.")
        else:
            msg = "No suspicious edges found — every edge tracks the scan."
        self._status.setText(msg)
        if announce:
            QMessageBox.information(self, "Missed-corner check", msg)

    # -- semi-automated tracing assist (Milestone 18) -----------------------

    def _set_assist_checked(self, on: bool) -> None:
        self._assist_action.blockSignals(True)
        self._assist_action.setChecked(on)
        self._assist_action.blockSignals(False)

    def _sync_assist_action(self) -> None:
        """Line-follow is offered for pixel sources only — disabled for DXF (exact
        vector geometry has no scanned line to follow; manual tracing + snapping is
        the right tool there, same reasoning as M17's missing-corner skip)."""
        ok = self.canvas.has_image() and not self._source_is_dxf()
        self._assist_action.setEnabled(ok)

    def begin_assist(self) -> None:
        """Menu/programmatic entry: latch the checkable toggle."""
        if self._assist_action.isChecked():
            self._on_assist_toggled(True)
        else:
            self._assist_action.setChecked(True)

    def _on_assist_toggled(self, checked: bool) -> None:
        if not checked:
            if self.canvas.is_assisting():
                self.canvas.stop_assist()
            self._status.setText("Line-follow assist off.")
            return
        if not self.canvas.has_image():
            self._set_assist_checked(False)
            QMessageBox.information(self, "No document", "Open a PDF or image first.")
            return
        if self._source_is_dxf():
            self._set_assist_checked(False)
            QMessageBox.information(
                self, "Not for DXF",
                "Line-follow assist is for scanned/rendered pixel sources. A DXF's "
                "boundary is exact vector geometry with no scanned line to follow — "
                "trace it manually (snapping still applies).")
            return
        if not self.canvas.start_assist():
            self._set_assist_checked(False)
            return
        # Mutually exclusive with the other checkable modes.
        self._set_select_checked(False)
        self._set_segment_checked(False)
        self._set_location_checked(False)
        self._sync_crosshair_action()
        self._status.setText(
            "Line-follow: click the start of a boundary line, then its end. The "
            "followed path is shown — Enter to accept, Esc to reject (then trace "
            "manually if it's wrong).")

    def _on_assist_requested(self, start, end) -> None:
        """Both ends marked: compute the followed path and show it for confirmation.
        Never auto-accepts — the user presses Enter/Esc (or uses the Boundary menu)."""
        if self._raw_raster is None:
            return
        from ..io.tracing_assist import follow_line, AssistUnavailable
        try:
            path = follow_line(self._raw_raster, (start.x(), start.y()), (end.x(), end.y()))
        except AssistUnavailable as exc:
            # Points too far apart for a bounded search — fall back to manual.
            QMessageBox.information(
                self, "Assist unavailable",
                f"{exc}.\n\nMark two points closer together along the line, or trace "
                "this segment manually.")
            self.canvas.clear_assist_preview()
            return
        except Exception as exc:   # degenerate raster etc. — never crash the UI
            QMessageBox.warning(self, "Could not follow line", str(exc))
            self.canvas.clear_assist_preview()
            return
        self.canvas.set_assist_preview(path)
        self._status.setText(
            f"Followed path: {len(path)} point(s). Enter to accept into the boundary, "
            "Esc to reject and trace manually.")

    def accept_assist(self) -> None:
        """Accept the previewed followed path (menu path; Enter does the same)."""
        if self.canvas.accept_assist_preview():
            self._status.setText("Followed path added to the boundary.")

    def reject_assist(self) -> None:
        """Reject the previewed path — the boundary is left completely unchanged."""
        self.canvas.clear_assist_preview()
        self._status.setText("Followed path rejected — boundary unchanged.")

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

    # -- boundary-segment report (Milestone 12) -----------------------------

    def begin_segment_description(self) -> None:
        """Menu/programmatic entry: latch the checkable toggle (drives
        :meth:`_on_segment_toggled`)."""
        if self._segment_action.isChecked():
            self._on_segment_toggled(True)
        else:
            self._segment_action.setChecked(True)

    def _set_segment_checked(self, on: bool) -> None:
        self._segment_action.blockSignals(True)
        self._segment_action.setChecked(on)
        self._segment_action.blockSignals(False)

    def _on_segment_toggled(self, checked: bool) -> None:
        """Enter/leave boundary-segment selection on the active parcel. Scale-first:
        a boundary description needs real lengths, so a parcel with no scale is
        blocked here rather than producing a fabricated number."""
        if not checked:
            if self.canvas.is_segment_selecting():
                self.canvas.stop_segment_selection()
            self._status.setText("Boundary-description mode off.")
            return

        if self._project is None or self._active_parcel_id is None:
            self._set_segment_checked(False)
            QMessageBox.information(
                self, "No parcel",
                "Select a parcel first — a boundary description is per parcel.")
            return
        if self._project.get_source_scale(self._source_id) is None:
            self._set_segment_checked(False)
            QMessageBox.information(
                self, "No scale set",
                "Set a scale for this source first — a boundary description reports "
                "real segment lengths, which need a scale (Scale-first rule).")
            return
        try:
            self._load_segment_edges()
        except segment_export.SegmentReportError as exc:
            self._set_segment_checked(False)
            QMessageBox.information(self, "No boundary", str(exc))
            return
        if not self.canvas.start_segment_selection():
            self._set_segment_checked(False)
            QMessageBox.information(
                self, "No boundary",
                "This parcel has no boundary edges to describe yet.")
            return
        self._set_select_checked(False)
        self._set_location_checked(False)
        self._set_assist_checked(False)
        self._sync_crosshair_action()
        self._refresh_segment_table()
        self._status.setText(
            "Describe boundary: click edges of the active parcel to build a "
            "contiguous run; type each neighbour in the side table; then Generate.")

    def _load_segment_edges(self) -> None:
        """Pull the active parcel's candidate edges (numbering matches the export)
        and reset the per-edge neighbour text."""
        self._segment_edges, self._segment_context = segment_export.list_parcel_edges(
            self._project, self._active_parcel_id)
        self._segment_neighbours = {}

    def _on_segment_selection_changed(self) -> None:
        self._refresh_segment_table()

    def _refresh_segment_table(self) -> None:
        """Rebuild the side table from the canvas's selected edge run, preserving
        any neighbour text already typed. This table's rows ARE the report."""
        selected = self.canvas.selected_segment_edges()
        by_index = {e.edge_index: e for e in self._segment_edges}
        self._segment_row_edges = list(selected)

        table = self._segment_table
        table.blockSignals(True)         # our own edits shouldn't fire itemChanged
        table.setRowCount(len(selected))
        total_m = 0.0
        for row, idx in enumerate(selected):
            edge = by_index.get(idx)
            if edge is None:
                continue
            length = edge.length_m
            total_m += length or 0.0
            values = [
                str(row + 1),
                edge.vertex_a_label,
                edge.vertex_b_label,
                "—" if length is None else f"{length:.2f} m",
                f"{edge.bearing_deg:.0f}° {edge.compass}",
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)   # read-only
                table.setItem(row, col, item)
            neighbour = QTableWidgetItem(self._segment_neighbours.get(idx, ""))
            neighbour.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, self._SEG_NEIGHBOUR_COL, neighbour)
        table.blockSignals(False)

        if selected:
            total_ft = total_m / 0.3048
            self._segment_total_label.setText(
                f"Total selected ({len(selected)} seg): {total_m:.2f} m ({total_ft:.1f} ft)")
        else:
            self._segment_total_label.setText("Total selected: —")
        self._segment_generate_btn.setEnabled(bool(selected))
        self._segment_clear_btn.setEnabled(bool(selected))

    def _on_segment_neighbour_edited(self, item) -> None:
        if item.column() != self._SEG_NEIGHBOUR_COL:
            return
        row = item.row()
        if 0 <= row < len(getattr(self, "_segment_row_edges", [])):
            self._segment_neighbours[self._segment_row_edges[row]] = item.text().strip()

    def _on_edge_hovered(self, edge_index: int) -> None:
        """Live 'hover an edge to see its length' readout (a general sanity tool)."""
        if edge_index < 0:
            return
        for edge in self._segment_edges:
            if edge.edge_index == edge_index:
                length = ("no scale" if edge.length_m is None
                          else f"{edge.length_m:.2f} m")
                self._status.setText(
                    f"Edge {edge.vertex_a_label}→{edge.vertex_b_label}: {length}, "
                    f"{edge.bearing_deg:.0f}° {edge.compass}")
                return

    def _clear_segment_selection(self) -> None:
        self.canvas.clear_segment_selection()   # emits change -> table refreshes

    def generate_segment_report(self, formats=None) -> None:
        """Export the boundary-description report for the current selection."""
        if self._project is None or self._active_parcel_id is None:
            return
        edge_indices = self.canvas.selected_segment_edges()
        if not edge_indices:
            QMessageBox.information(
                self, "No segments",
                "Select at least one boundary edge on the canvas first.")
            return
        if formats is None:
            formats = self._ask_report_formats()
            if formats is None:
                return
        try:
            report, paths = segment_export.export_segment_report(
                self._project, self._active_parcel_id, self._project.exports_dir,
                edge_indices=list(edge_indices), neighbours=dict(self._segment_neighbours),
                formats=formats)
        except (segment_export.SegmentReportError, ValueError) as exc:
            QMessageBox.warning(self, "Could not generate", str(exc))
            return
        except Exception as exc:   # e.g. PyMuPDF missing for PDF
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        names = "\n".join(f"  • {p.name}" for p in paths)
        QMessageBox.information(
            self, "Boundary description generated",
            f"{report.segment_count} segment(s), total {report.total_length_m:.2f} m, "
            f"written to {self._project.exports_dir}:\n{names}")
        self._status.setText(
            f"Generated boundary description → {len(paths)} file(s) in exports/")

    def _ask_report_formats(self):
        """Small PDF-default / CSV-JSON-optional chooser (matches the M11 policy).
        Returns the chosen formats, or None if cancelled."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Generate boundary description")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Formats (written to the project's exports/ folder):"))
        layout.addWidget(QLabel("PDF (primary) — always generated."))
        csv = QCheckBox("Also write CSV")
        js = QCheckBox("Also write JSON")
        layout.addWidget(csv)
        layout.addWidget(js)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Generate")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if not dlg.exec():
            return None
        formats = ["pdf"]
        if csv.isChecked():
            formats.append("csv")
        if js.isChecked():
            formats.append("json")
        return formats

    # -- reports (Milestone 11) ---------------------------------------------

    def export_owner_report(self) -> None:
        """Generate the owner-wise summary (parcels grouped by owner, with
        per-owner and grand-total area) as PDF / CSV / JSON into the project's
        ``exports/`` folder. Scope follows M7's selection: if a non-empty parcel
        selection is active it is used, otherwise every parcel in the project.
        All report logic lives in ``src.export.report``; this only chooses the
        scope, previews coverage, and writes files."""
        if self._project is None:
            QMessageBox.information(
                self, "No project",
                "Open or create a project first — reports cover a project's parcels.")
            return

        # Reuse M7's selection hook: a non-empty selection scopes the report;
        # an empty selection means "all parcels in the project".
        selected = self.selected_parcel_ids()
        parcel_ids = set(selected) if selected else None
        scope_label = (f"{len(selected)} selected parcel(s) on this sheet"
                       if selected else "all parcels in the project")

        report = report_export.build_owner_report(self._project, parcel_ids=parcel_ids)
        if report.parcel_count == 0:
            QMessageBox.information(
                self, "Nothing to report",
                "No parcels found for this report. Trace a parcel boundary first.")
            return

        # Preview coverage (owners/parcels) and pick formats before generating.
        dlg = ReportDialog(report, scope_label, self)
        if not dlg.exec():
            return
        formats = dlg.selected_formats()   # always includes PDF (primary)

        # One file per owner per format, written to never-overwriting timestamped
        # paths in exports/ (brief storage rule). All of this lives in report.py.
        try:
            _report, report_paths, image_paths = report_export.export_owner_reports(
                self._project, self._project.exports_dir,
                formats=formats, parcel_ids=parcel_ids)
        except Exception as exc:   # e.g. PyMuPDF missing for PDF, or a write error
            QMessageBox.warning(self, "Export failed", str(exc))
            return

        written = report_paths + image_paths
        names = "\n".join(f"  • {p.name}" for p in written)
        extra = (f"\n\n{report.missing_scale_count} parcel(s) had no scale set and "
                 "were excluded from area totals." if report.missing_scale_count else "")
        img_note = (f"\n({len(image_paths)} boundary image file(s) saved separately.)"
                    if image_paths else "")
        QMessageBox.information(
            self, "Report generated",
            f"Owner-wise summary written to {self._project.exports_dir}:\n{names}\n\n"
            f"{len(report_paths)} report file(s) — one per owner "
            f"({report.owner_count} owner(s)) × {len(formats)} format(s).{img_note}{extra}")
        self._status.setText(
            f"Generated owner-wise report → {len(report_paths)} file(s) in exports/")

    # -- readouts -----------------------------------------------------------

    def _update_scale_readout(self) -> None:
        if self._scale is None:
            self._scale_readout.setText("Scale: not set")
        else:
            s = self._scale
            source = ""
            if s.method == METHOD_PDF_METADATA:
                source = " [PDF metadata]"
            elif s.method == METHOD_DXF_HEADER:
                source = " [DXF header]"
            self._scale_readout.setText(
                f"Scale: 1 px = {s.metres_per_pixel:.4g} m   "
                f"(1 m = {s.pixels_per_metre:.4g} px){source}")

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
