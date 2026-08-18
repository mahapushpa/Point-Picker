"""canvas_view — native QGraphicsView canvas with pan/zoom (PySide6).

Thin UI: display a RasterImage, handle pan/zoom, and — for scale calibration
(Milestone 3) — let the user click two points. No domain logic (the scale math
lives in ``src.core.scale``). Interaction is ported (not copied) from
reference/point_picker.html:

  * scroll wheel zooms, anchored on the point under the cursor (Qt's
    ``AnchorUnderMouse`` transformation anchor reproduces the prototype's
    zoom-to-cursor math natively);
  * left-button drag pans;
  * a left-button *click* (press+release with no meaningful movement) marks a
    point — exactly the prototype's click-vs-drag distinction via a small
    movement threshold. Marking is only armed during a calibration; normal
    viewing is unaffected.

Only the two-point scale pick is wired up here. General polygon point-marking is
Milestone 4.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QGraphicsScene, QGraphicsView, QGraphicsEllipseItem, QGraphicsLineItem,
    QGraphicsSimpleTextItem, QGraphicsItem,
)

from ..io.raster import RasterImage

_MARK_COLOR = QColor("#1D9E75")  # same accent green as point_picker.html


def qimage_from_raster(raster: RasterImage) -> QImage:
    """Build a QImage from a neutral RasterImage.

    ``QImage`` does not copy the buffer it is handed, so we ``.copy()`` to give
    the QImage ownership of an independent copy — otherwise the pixels would be
    freed when the source ``bytes`` is garbage-collected.
    """
    if raster.mode != "RGBA":
        raise ValueError(f"canvas expects RGBA rasters, got mode {raster.mode!r}")
    img = QImage(
        raster.data, raster.width, raster.height, raster.stride,
        QImage.Format.Format_RGBA8888,
    )
    return img.copy()


class CanvasView(QGraphicsView):
    """A pan/zoom viewport for a single rendered document."""

    MIN_SCALE = 0.05
    MAX_SCALE = 20.0
    ZOOM_STEP = 1.15          # matches point_picker.html's wheel step
    CLICK_MOVE_THRESHOLD = 4  # px of movement below which a release is a click

    #: Emitted (in image pixel coordinates) once two calibration points are set.
    twoPointsPicked = Signal(QPointF, QPointF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item = None
        self._scale = 1.0  # tracked absolute scale, for clamping

        # Mouse-gesture state (shared by pan and click-to-mark).
        self._mouse_down = False
        self._moved = False
        self._press_pos = None
        self._last_pan_pos = None

        # Scale-calibration state.
        self._calibrating = False
        self._calib_points: list[QPointF] = []
        self._calib_items: list[QGraphicsItem] = []

        # Zoom around the cursor (the prototype's zoom-to-cursor math).
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)  # we pan manually
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(Qt.GlobalColor.lightGray)

    # -- image display ------------------------------------------------------

    def set_image(self, raster: RasterImage) -> None:
        """Display *raster*, replacing anything already shown, and fit it."""
        pixmap = QPixmap.fromImage(qimage_from_raster(raster))
        self._scene.clear()  # also drops any calibration marker items
        self._calib_points.clear()
        self._calib_items.clear()
        self._calibrating = False
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self.reset_view()

    def has_image(self) -> bool:
        return self._pixmap_item is not None

    def reset_view(self) -> None:
        """Fit the whole image in the viewport (the prototype's 'Reset view')."""
        if self._pixmap_item is None:
            return
        self.resetTransform()
        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._scale = self.transform().m11()
        self._update_cursor()

    def zoom_in(self) -> None:
        self._zoom_by(self.ZOOM_STEP, anchor_center=True)

    def zoom_out(self) -> None:
        self._zoom_by(1 / self.ZOOM_STEP, anchor_center=True)

    # -- scale calibration --------------------------------------------------

    def start_scale_calibration(self) -> bool:
        """Arm two-point calibration: clear any previous markers and wait for
        two clicks. Returns False if there is no image to calibrate against."""
        if self._pixmap_item is None:
            return False
        self.clear_scale_markers()
        self._calibrating = True
        self._update_cursor()
        return True

    def cancel_scale_calibration(self) -> None:
        self._calibrating = False
        self.clear_scale_markers()
        self._update_cursor()

    def clear_scale_markers(self) -> None:
        """Remove calibration markers/line and forget picked points."""
        for item in self._calib_items:
            if item.scene() is self._scene:
                self._scene.removeItem(item)
        self._calib_items.clear()
        self._calib_points.clear()

    def is_calibrating(self) -> bool:
        return self._calibrating

    # -- interaction --------------------------------------------------------

    def wheelEvent(self, event) -> None:
        if self._pixmap_item is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        self._zoom_by(self.ZOOM_STEP if delta > 0 else 1 / self.ZOOM_STEP)

    def _zoom_by(self, factor: float, *, anchor_center: bool = False) -> None:
        if self._pixmap_item is None:
            return
        new_scale = self._scale * factor
        # Clamp to the prototype's range; ignore a step that would exceed it.
        if new_scale < self.MIN_SCALE or new_scale > self.MAX_SCALE:
            return
        self._scale = new_scale
        if anchor_center:
            prev = self.transformationAnchor()
            self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
            self.scale(factor, factor)
            self.setTransformationAnchor(prev)
        else:
            self.scale(factor, factor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pixmap_item is not None:
            self._mouse_down = True
            self._moved = False
            self._press_pos = event.position()
            self._last_pan_pos = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._mouse_down and self._last_pan_pos is not None:
            pos = event.position()
            if not self._moved:
                if (pos - self._press_pos).manhattanLength() > self.CLICK_MOVE_THRESHOLD:
                    self._moved = True  # became a drag, not a click
            if self._moved:
                # Pan by moving the scrollbars (image content follows the drag),
                # mirroring the prototype's panX/panY += drag delta.
                dx = pos.x() - self._last_pan_pos.x()
                dy = pos.y() - self._last_pan_pos.y()
                h, v = self.horizontalScrollBar(), self.verticalScrollBar()
                h.setValue(h.value() - int(dx))
                v.setValue(v.value() - int(dy))
            self._last_pan_pos = pos
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._mouse_down:
            self._mouse_down = False
            was_click = not self._moved
            self._update_cursor()
            if was_click:
                self._handle_click(event.position())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- click-to-mark ------------------------------------------------------

    def _handle_click(self, view_pos: QPointF) -> None:
        """A genuine click (no drag). Only meaningful while calibrating."""
        if not self._calibrating or self._pixmap_item is None:
            return
        scene_pt = self.mapToScene(view_pos.toPoint())  # == image pixel coords
        self._calib_points.append(scene_pt)
        self._add_calib_marker(scene_pt, len(self._calib_points))
        if len(self._calib_points) == 2:
            self._add_calib_line(self._calib_points[0], self._calib_points[1])
            self._calibrating = False
            self._update_cursor()
            self.twoPointsPicked.emit(self._calib_points[0], self._calib_points[1])

    def _add_calib_marker(self, scene_pt: QPointF, number: int) -> None:
        r = 6.0
        pen = QPen(_MARK_COLOR)
        pen.setWidth(2)
        pen.setCosmetic(True)  # constant 2px stroke regardless of zoom
        dot = QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
        dot.setPen(pen)
        dot.setPos(scene_pt)
        # Keep markers a constant on-screen size at any zoom level.
        dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self._scene.addItem(dot)
        self._calib_items.append(dot)

        label = QGraphicsSimpleTextItem(str(number))
        label.setBrush(_MARK_COLOR)
        label.setPos(scene_pt.x(), scene_pt.y())
        label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        label.moveBy(r + 2, -(r + 12))
        self._scene.addItem(label)
        self._calib_items.append(label)

    def _add_calib_line(self, a: QPointF, b: QPointF) -> None:
        pen = QPen(_MARK_COLOR)
        pen.setWidth(2)
        pen.setCosmetic(True)
        line = QGraphicsLineItem(a.x(), a.y(), b.x(), b.y())
        line.setPen(pen)
        self._scene.addItem(line)
        self._calib_items.append(line)

    # -- helpers ------------------------------------------------------------

    def _update_cursor(self) -> None:
        if self._pixmap_item is None:
            self.unsetCursor()
        elif self._calibrating:
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
