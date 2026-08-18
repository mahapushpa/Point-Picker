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
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QLabel, QMainWindow, QMessageBox,
    QToolBar,
)

from ..core.geometry import measure_polygon
from ..core.project_db import ProjectDB, ProjectError
from ..core.scale import compute_two_point_scale, TwoPointScale
from ..io.raster import open_raster
from .canvas_view import CanvasView

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

        # Session state.
        self._project: ProjectDB | None = None
        self._current_path: str | None = None
        self._source_id: int | None = None
        self._scale: TwoPointScale | None = None  # in-memory; mirrors the DB when a project is open

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

        scale_menu = self.menuBar().addMenu("&Scale")
        set_scale_act = QAction("&Set scale (two points)…", self)
        set_scale_act.triggered.connect(self.begin_scale_calibration)
        scale_menu.addAction(set_scale_act)
        clear_scale_act = QAction("&Clear scale", self)
        clear_scale_act.triggered.connect(self.clear_scale)
        scale_menu.addAction(clear_scale_act)

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
        self._project_readout.setText(f"Project: {proj.get_meta('project_name')}")
        self._update_title()

    def _attach_source_to_project(self) -> None:
        """Register the loaded file into the open project (idempotent), then
        either restore its saved scale/boundary or push the current in-memory
        ones into the freshly registered source."""
        if self._project is None or self._current_path is None:
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

        db_poly = self._project.get_polygon(sid)
        if db_poly:
            # Restore the exact saved open/closed state, not one inferred from
            # the point count — an open 3+-point boundary stays open.
            self.canvas.set_polygon(db_poly, closed=self._project.get_polygon_closed(sid))
        elif self.canvas.polygon_points():
            self._persist_polygon()                     # push in-memory boundary

        self._update_scale_readout()
        self._update_measure_readout()

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
        self.canvas.set_image(raster)   # clears any prior markers/boundary
        self._current_path = str(path)
        self._source_id = None
        self._scale = None              # a new file has its own scale/boundary
        self._attach_source_to_project()
        self._update_scale_readout()
        self._update_measure_readout()
        self._status.setText(f"{Path(path).name}   —   {raster.width} × {raster.height} px")
        self._update_title()

    # -- scale calibration --------------------------------------------------

    def begin_scale_calibration(self) -> None:
        if not self.canvas.start_scale_calibration():
            QMessageBox.information(self, "No document", "Open a PDF or image first.")
            return
        self._sync_crosshair_action()
        self._status.setText(
            "Set scale: click two points a known distance apart; drag or arrow-keys "
            "to fine-tune, Enter to confirm, Esc to cancel.")

    def clear_scale(self) -> None:
        self.canvas.cancel_scale_calibration()
        self._scale = None
        if self._project is not None and self._source_id is not None:
            self._project.clear_source_scale(self._source_id)
            self._persist_polygon()  # boundary local coords no longer have a scale
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
        self._persist_polygon()  # refresh boundary's SI coordinates with new scale

    def _persist_polygon(self) -> None:
        if self._project is None or self._source_id is None:
            return
        pts = self.canvas.polygon_points()
        mpp = self._scale.metres_per_pixel if self._scale is not None else None
        if pts:
            self._project.save_polygon(self._source_id, pts,
                                       closed=self.canvas.is_polygon_closed(),
                                       metres_per_pixel=mpp)
        else:
            self._project.clear_polygon(self._source_id)

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
            seg = f"{m.last_segment_m:.3g} m" if m.last_segment_m is not None else "—"
            perim = f"{m.perimeter_m:.4g} m"
            area = f"{m.area_sq_m:.4g} m²" if n >= 3 else "—"
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
