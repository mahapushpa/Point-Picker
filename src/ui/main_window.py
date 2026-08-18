"""main_window — minimal application shell (PySide6).

Thin UI: open a PDF/PNG/JPEG via a file dialog, hand it to io/ to decode, show
the result in the pan/zoom canvas, and drive two-point scale calibration. No
domain logic lives here — decoding is ``src.io``'s job, interaction is the
canvas's job, and the scale arithmetic is ``src.core.scale``'s job. This window
only wires them together, prompts for the real-world distance, and displays the
result.

Milestone 3 scope: load + display + pan/zoom + manual two-point scale (SI only,
held in memory). No polygon tracing, area/perimeter, unit profiles, or DXF.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QLabel, QMainWindow, QMessageBox,
    QToolBar,
)

from ..core.scale import compute_two_point_scale, TwoPointScale
from ..io.raster import open_raster
from .canvas_view import CanvasView

# File-dialog filters. Kept in sync with what src.io can actually render today.
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
        self.resize(1000, 720)

        self.canvas = CanvasView(self)
        self.setCentralWidget(self.canvas)
        self.canvas.twoPointsPicked.connect(self._on_two_points_picked)

        # In-memory scale for the currently loaded file (Milestone 3 keeps this
        # in memory only; DB persistence is exercised at the project_db layer).
        self._scale: TwoPointScale | None = None

        self._status = QLabel("Open a PDF or image to begin.")
        self.statusBar().addWidget(self._status)
        self._scale_readout = QLabel("Scale: not set")
        self.statusBar().addPermanentWidget(self._scale_readout)

        self._build_menu()
        self._build_toolbar()

    def _build_toolbar(self) -> None:
        """Always-visible zoom controls, so the constantly-used +/-/reset
        actions don't require opening the View menu. These trigger exactly the
        same canvas methods (and therefore the same 0.05x-20x clamp and
        anchor-under-cursor behaviour) as the View menu items — this only
        exposes them more directly, it changes no zoom logic."""
        bar = QToolBar("Zoom", self)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)

        for text, tooltip, slot in (
            ("−", "Zoom out", self.canvas.zoom_out),   # minus sign
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

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        open_act = QAction("&Open…", self)
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

        scale_menu = self.menuBar().addMenu("&Scale")
        set_scale_act = QAction("&Set scale (two points)…", self)
        set_scale_act.triggered.connect(self.begin_scale_calibration)
        scale_menu.addAction(set_scale_act)
        clear_scale_act = QAction("&Clear scale", self)
        clear_scale_act.triggered.connect(self.clear_scale)
        scale_menu.addAction(clear_scale_act)

    # -- actions ------------------------------------------------------------

    def open_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open document", "", _FILE_FILTER)
        if path:
            self.load_path(path)

    def load_path(self, path) -> None:
        """Decode *path* via io/ and show it. Errors surface as a dialog."""
        try:
            raster = open_raster(path)
        except Exception as exc:  # decoding errors are user-facing, not crashes
            QMessageBox.critical(self, "Could not open file", f"{Path(path).name}\n\n{exc}")
            self._status.setText("Open a PDF or image to begin.")
            return
        self.canvas.set_image(raster)
        # A new file has its own scale; discard the previous one.
        self._scale = None
        self._update_scale_readout()
        self._status.setText(f"{Path(path).name}   —   {raster.width} × {raster.height} px")
        self.setWindowTitle(f"Land Measure Tool — {Path(path).name}")

    # -- scale calibration --------------------------------------------------

    def begin_scale_calibration(self) -> None:
        """Arm two-point calibration. Clicking this again is the 'redo': it
        clears the previous markers and lets the user re-pick two points."""
        if not self.canvas.start_scale_calibration():
            QMessageBox.information(self, "No document", "Open a PDF or image first.")
            return
        self._status.setText(
            "Set scale: click the first point, then the second, on a known distance."
        )

    def clear_scale(self) -> None:
        self.canvas.cancel_scale_calibration()
        self._scale = None
        self._update_scale_readout()
        self._status.setText("Scale cleared.")

    def _on_two_points_picked(self, p1, p2) -> None:
        """Both calibration points are set; ask the real distance and compute."""
        distance, ok = QInputDialog.getDouble(
            self, "Real-world distance",
            "Distance between the two points (metres):",
            1.0, 0.0000001, 1_000_000.0, 4,
        )
        if not ok:
            # User cancelled: drop the markers, leave any prior scale untouched.
            self.canvas.clear_scale_markers()
            self._status.setText("Scale calibration cancelled.")
            return
        try:
            scale = compute_two_point_scale(
                (p1.x(), p1.y()), (p2.x(), p2.y()), distance,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Could not set scale", str(exc))
            self.canvas.clear_scale_markers()
            self._status.setText("Scale not set — pick two distinct points.")
            return
        self._scale = scale
        self._update_scale_readout()
        self._status.setText(
            f"Scale set from {scale.pixel_distance:.1f} px = {distance:g} m."
        )

    def _update_scale_readout(self) -> None:
        if self._scale is None:
            self._scale_readout.setText("Scale: not set")
        else:
            s = self._scale
            self._scale_readout.setText(
                f"Scale: 1 px = {s.metres_per_pixel:.4g} m   "
                f"(1 m = {s.pixels_per_metre:.4g} px)"
            )


def run(file: str | None = None, argv=None) -> int:
    """Launch the GUI. Optionally open *file* on startup. Returns the Qt exit code."""
    app = QApplication.instance() or QApplication(argv or [])
    win = MainWindow()
    if file:
        win.load_path(file)
    win.show()
    return app.exec()
