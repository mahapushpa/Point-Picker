"""main_window — minimal application shell (PySide6).

Thin UI: open a PDF/PNG/JPEG via a file dialog, hand it to io/ to decode, and
show the result in the pan/zoom canvas. No domain logic lives here — decoding is
``src.io``'s job, interaction is the canvas's job. This window only wires them
together and offers zoom controls.

Milestone 2 scope: load + display + pan/zoom. No point marking, scale, or DXF.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QLabel, QMainWindow, QMessageBox,
)

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

        self._status = QLabel("Open a PDF or image to begin.")
        self.statusBar().addWidget(self._status)

        self._build_menu()

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
        self._status.setText(f"{Path(path).name}   —   {raster.width} × {raster.height} px")
        self.setWindowTitle(f"Land Measure Tool — {Path(path).name}")


def run(file: str | None = None, argv=None) -> int:
    """Launch the GUI. Optionally open *file* on startup. Returns the Qt exit code."""
    app = QApplication.instance() or QApplication(argv or [])
    win = MainWindow()
    if file:
        win.load_path(file)
    win.show()
    return app.exec()
