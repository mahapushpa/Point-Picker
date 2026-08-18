"""canvas_view — native QGraphicsView canvas with pan/zoom + point picking (PySide6).

Thin UI: display a RasterImage, pan/zoom, and let the user pick points for the
two shared workflows — two-point scale calibration (M3) and polygon boundary
tracing (M4). No domain logic (scale/geometry math live in ``src.core``).

Point picking is a single shared model used by both modes (M4.5 refinements),
so corrections work identically everywhere:

  * **place** — a left click (press+release under the movement threshold) adds a
    point; a left drag on empty space still pans;
  * **adjust** — press on an already-placed point and drag it, or select it and
    nudge with the arrow keys (1 px, or 10 px with Shift), before finalising;
  * **confirm** — Enter finalises the pick (scale: emit the two points so the
    distance can be entered; polygon: close the boundary);
  * **cancel** — Esc fully resets the in-progress pick with no residual points
    or half-armed mode.

Zoom/pan behaviour is ported from reference/point_picker.html (wheel zoom
anchored under the cursor, drag-to-pan, 0.05x..20x clamp).

A full-window precision crosshair (CAD/GIS style: horizontal + vertical lines
spanning the whole viewport, drawn with a dark core and light halo for contrast
on any map background) can be toggled while picking. It is drawn in viewport
pixels via :meth:`drawForeground`, so it is a fixed on-screen overlay independent
of zoom — the arms always span the window and stay usable at any zoom level.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QBrush
from PySide6.QtWidgets import (
    QGraphicsScene, QGraphicsView, QGraphicsEllipseItem, QGraphicsLineItem,
    QGraphicsSimpleTextItem, QGraphicsItem,
)

from ..io.raster import RasterImage

_MARK_COLOR = QColor("#1D9E75")  # scale-calibration markers: accent green (as point_picker.html)
_POLY_COLOR = QColor("#E8770F")  # boundary-tracing markers: orange, visually distinct
_ACTIVE_HALO = QColor("#FFFFFF")  # ring around the currently-selected point


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
    ZOOM_STEP = 1.15           # matches point_picker.html's wheel step
    CLICK_MOVE_THRESHOLD = 4   # px of movement below which a release is a click
    POINT_HIT_RADIUS = 12      # px around a marker that counts as grabbing it
    NUDGE_STEP = 1.0           # image px per arrow press
    NUDGE_STEP_SHIFT = 10.0    # image px per Shift+arrow press

    #: Emitted (in image pixel coordinates) when a two-point scale is confirmed.
    twoPointsPicked = Signal(QPointF, QPointF)
    #: Emitted whenever the traced polygon changes (add / adjust / undo / clear / close).
    polygonChanged = Signal()
    #: Emitted when the polygon is closed (>= 3 points).
    polygonClosed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item = None
        self._scale = 1.0  # tracked absolute zoom scale, for clamping

        # Gesture state.
        self._mouse_down = False
        self._moved = False
        self._press_pos = None
        self._last_pan_pos = None
        self._drag_kind: str | None = None   # 'scale' | 'poly' while dragging a marker
        self._drag_index: int | None = None
        self._active: tuple[str, int] | None = None  # selected marker for arrow-nudge

        # Scale-calibration state.
        self._calibrating = False
        self._calib_points: list[QPointF] = []
        self._calib_items: list[QGraphicsItem] = []

        # Polygon-tracing state (the one active, editable boundary).
        self._tracing = False
        self._poly_points: list[QPointF] = []
        self._poly_closed = False
        self._poly_items: list[QGraphicsItem] = []
        self._active_color = _POLY_COLOR
        # Other parcels of the same source, drawn for context (not editable).
        self._bg_items: list[QGraphicsItem] = []

        # Precision crosshair.
        self._crosshair_enabled = False
        self._cursor_vp_pos = None  # QPoint in viewport coords, or None

        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)  # we pan manually
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(Qt.GlobalColor.lightGray)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # for Enter/Esc/arrow keys
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)  # crosshair follows the free cursor

    # -- image display ------------------------------------------------------

    def set_image(self, raster: RasterImage) -> None:
        """Display *raster*, replacing anything already shown, and fit it."""
        pixmap = QPixmap.fromImage(qimage_from_raster(raster))
        self._scene.clear()  # also drops any calibration / polygon marker items
        self._reset_calibration_state()
        self._reset_polygon_state()
        self._bg_items.clear()  # scene.clear() already removed them
        self._active = None
        self._drag_kind = self._drag_index = None
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
        """Arm two-point calibration: clear any previous markers and wait for two
        clicks (then Enter to confirm). Returns False if there is no image."""
        if self._pixmap_item is None:
            return False
        self._reset_polygon_mode()          # modes are mutually exclusive
        self.clear_scale_markers()
        self._calibrating = True
        self._crosshair_enabled = True       # crosshair on by default for scale
        self.setFocus()
        self._update_cursor()
        self.viewport().update()
        return True

    def cancel_scale_calibration(self) -> None:
        self._reset_calibration_state()
        self._update_cursor()
        self.viewport().update()

    def clear_scale_markers(self) -> None:
        """Remove calibration markers/line and forget picked points."""
        self._remove_items(self._calib_items)
        self._calib_points.clear()
        if self._active is not None and self._active[0] == "scale":
            self._active = None

    def is_calibrating(self) -> bool:
        return self._calibrating

    # -- polygon tracing ----------------------------------------------------

    def start_polygon(self) -> bool:
        """Enter boundary-tracing mode. Keeps any existing polygon so tracing can
        resume; use :meth:`clear_polygon` to start over. Returns False if there
        is no image."""
        if self._pixmap_item is None:
            return False
        self._reset_calibration_mode()       # modes are mutually exclusive
        if self._poly_closed:
            self._poly_closed = False        # re-open to continue editing
        self._tracing = True
        self._crosshair_enabled = False      # off by default for polygon (many vertices)
        self.setFocus()
        self._redraw_polygon()
        self._update_cursor()
        self.viewport().update()
        return True

    def stop_polygon(self) -> None:
        self._tracing = False
        self._update_cursor()
        self.viewport().update()

    def undo_last_point(self) -> None:
        if not self._poly_points:
            return
        self._poly_points.pop()
        self._poly_closed = False
        if self._active is not None and self._active[0] == "poly":
            self._active = ("poly", len(self._poly_points) - 1) if self._poly_points else None
        self._redraw_polygon()
        self.polygonChanged.emit()

    def clear_polygon(self) -> None:
        had_any = bool(self._poly_points)
        self._poly_points.clear()
        self._poly_closed = False
        if self._active is not None and self._active[0] == "poly":
            self._active = None
        self._redraw_polygon()
        if had_any:
            self.polygonChanged.emit()

    def close_polygon(self) -> bool:
        """Close the boundary (last point joins the first). Needs >= 3 points."""
        if len(self._poly_points) < 3 or self._poly_closed:
            return False
        self._poly_closed = True
        self._tracing = False
        self._redraw_polygon()
        self._update_cursor()
        self.polygonChanged.emit()
        self.polygonClosed.emit()
        return True

    def set_polygon(self, pixel_points, closed: bool = True) -> None:
        """Replace the polygon with points restored from storage (image pixels).
        Does not emit change signals — the caller is loading, not editing."""
        self._poly_points = [QPointF(float(x), float(y)) for x, y in pixel_points]
        self._poly_closed = closed and len(self._poly_points) >= 3
        self._tracing = False
        self._active = None
        self._redraw_polygon()
        self._update_cursor()

    def polygon_points(self) -> list[tuple[float, float]]:
        """Current boundary as ordered (x, y) image-pixel tuples."""
        return [(p.x(), p.y()) for p in self._poly_points]

    def is_polygon_closed(self) -> bool:
        return self._poly_closed

    def is_tracing(self) -> bool:
        return self._tracing

    def set_active_color(self, color: QColor) -> None:
        """Colour of the active (editable) boundary. Set per parcel so each is
        visually distinct from the others shown in the background."""
        self._active_color = QColor(color)
        self._redraw_polygon()

    def set_background_polygons(self, polygons) -> None:
        """Draw other parcels of the same source for context, non-interactively.
        *polygons* is a list of ``(points, closed, color, label)`` where points
        are (x, y) image-pixel tuples. These are visuals only — hit-testing and
        editing always target the single active boundary."""
        self._remove_items(self._bg_items)
        for points, closed, color, label in polygons:
            if not points:
                continue
            qcolor = QColor(color)
            qcolor.setAlpha(210)  # slightly muted so the active boundary stands out
            pen = QPen(qcolor)
            pen.setWidth(2)
            pen.setCosmetic(True)
            qpts = [QPointF(float(x), float(y)) for x, y in points]
            segments = list(zip(qpts, qpts[1:]))
            if closed and len(qpts) >= 3:
                segments.append((qpts[-1], qpts[0]))
            for a, b in segments:
                line = QGraphicsLineItem(a.x(), a.y(), b.x(), b.y())
                line.setPen(pen)
                line.setZValue(1)  # above the pixmap, below the active boundary
                self._scene.addItem(line)
                self._bg_items.append(line)
            if label:
                text = QGraphicsSimpleTextItem(label)
                text.setBrush(QColor(color))
                text.setPos(qpts[0])
                text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
                text.setZValue(2)
                self._scene.addItem(text)
                self._bg_items.append(text)

    # -- shared pick: confirm / cancel / crosshair --------------------------

    def confirm_pick(self) -> bool:
        """Finalise the in-progress pick (Enter). Scale: emit the two points.
        Polygon: close the boundary. No-op (returns False) if not enough points."""
        if self._calibrating and len(self._calib_points) == 2:
            self._calibrating = False
            self._active = None
            self._update_cursor()
            self.viewport().update()
            self.twoPointsPicked.emit(self._calib_points[0], self._calib_points[1])
            return True
        if self._tracing and len(self._poly_points) >= 3:
            return self.close_polygon()
        return False

    def cancel_pick(self) -> bool:
        """Fully reset whatever pick is in progress (Esc). Returns True if there
        was something to cancel. Leaves no residual points or half-armed mode."""
        if self._calibrating:
            self._reset_calibration_state()
            self._update_cursor()
            self.viewport().update()
            return True
        if self._tracing:
            self.clear_polygon()             # drops all points + emits change
            self._tracing = False
            self._update_cursor()
            self.viewport().update()
            return True
        return False

    def set_crosshair_enabled(self, enabled: bool) -> None:
        self._crosshair_enabled = bool(enabled)
        self._update_cursor()
        self.viewport().update()

    def is_crosshair_enabled(self) -> bool:
        return self._crosshair_enabled

    def toggle_crosshair(self) -> None:
        self.set_crosshair_enabled(not self._crosshair_enabled)

    def is_picking(self) -> bool:
        return self._calibrating or self._tracing

    # -- zoom ---------------------------------------------------------------

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
        self.viewport().update()

    # -- mouse --------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pixmap_item is not None:
            pos = event.position()
            hit = self._marker_at(pos) if self.is_picking() else None
            if hit is not None:
                # Grab an existing point to fine-tune it (drag), not pan/place.
                self._drag_kind, self._drag_index = hit
                self._active = hit
                self._redraw_active()
                event.accept()
                return
            # Ambiguous: becomes a pan if it moves, a placement if it doesn't.
            self._mouse_down = True
            self._moved = False
            self._press_pos = pos
            self._last_pan_pos = pos
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        self._cursor_vp_pos = pos.toPoint()
        if self._crosshair_enabled and self.is_picking():
            self.viewport().update()

        if self._drag_index is not None:
            self._set_marker_position(self._drag_kind, self._drag_index,
                                      self.mapToScene(pos.toPoint()))
            event.accept()
            return

        if self._mouse_down and self._last_pan_pos is not None:
            if not self._moved and (pos - self._press_pos).manhattanLength() > self.CLICK_MOVE_THRESHOLD:
                self._moved = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._moved:
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
        if event.button() == Qt.MouseButton.LeftButton:
            if self._drag_index is not None:
                # Finished fine-tuning a point; persist the final position.
                if self._drag_kind == "poly":
                    self.polygonChanged.emit()
                self._drag_kind = self._drag_index = None
                self._update_cursor()
                event.accept()
                return
            if self._mouse_down:
                self._mouse_down = False
                was_click = not self._moved
                self._update_cursor()
                if was_click:
                    self._place_point(self.mapToScene(event.position().toPoint()))
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        self._cursor_vp_pos = None
        self.viewport().update()
        super().leaveEvent(event)

    # -- keyboard: confirm / cancel / nudge ---------------------------------

    def keyPressEvent(self, event) -> None:
        if self.is_picking():
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.confirm_pick()
                event.accept()
                return
            if key == Qt.Key.Key_Escape:
                self.cancel_pick()
                event.accept()
                return
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
                if self._active is not None:
                    step = (self.NUDGE_STEP_SHIFT
                            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                            else self.NUDGE_STEP)
                    dx = (-step if key == Qt.Key.Key_Left else step if key == Qt.Key.Key_Right else 0.0)
                    dy = (-step if key == Qt.Key.Key_Up else step if key == Qt.Key.Key_Down else 0.0)
                    self._nudge_active(dx, dy)
                    event.accept()
                    return
        super().keyPressEvent(event)

    # -- shared point editing (scene coordinates) ---------------------------

    def _place_point(self, scene_pt: QPointF) -> None:
        """Add a point at *scene_pt* for the active pick mode."""
        if self._calibrating:
            if len(self._calib_points) >= 2:
                return  # two points already placed; adjust or confirm/cancel
            self._calib_points.append(scene_pt)
            self._active = ("scale", len(self._calib_points) - 1)
            self._redraw_calibration()
        elif self._tracing:
            self._poly_points.append(scene_pt)
            self._active = ("poly", len(self._poly_points) - 1)
            self._redraw_polygon()
            self.polygonChanged.emit()

    def _set_marker_position(self, kind: str, index: int, scene_pt: QPointF,
                            *, emit: bool = True) -> None:
        if kind == "scale" and 0 <= index < len(self._calib_points):
            self._calib_points[index] = scene_pt
            self._active = ("scale", index)
            self._redraw_calibration()
        elif kind == "poly" and 0 <= index < len(self._poly_points):
            self._poly_points[index] = scene_pt
            self._active = ("poly", index)
            self._redraw_polygon()
            if emit:
                self.polygonChanged.emit()

    def _nudge_active(self, dx: float, dy: float) -> None:
        if self._active is None:
            return
        kind, index = self._active
        pts = self._calib_points if kind == "scale" else self._poly_points
        if 0 <= index < len(pts):
            p = pts[index]
            self._set_marker_position(kind, index, QPointF(p.x() + dx, p.y() + dy))

    def _marker_at(self, view_pos: QPointF):
        """Return ('scale'|'poly', index) of the nearest adjustable marker within
        POINT_HIT_RADIUS of *view_pos* (viewport coords), else None."""
        pts = self._calib_points if self._calibrating else self._poly_points
        kind = "scale" if self._calibrating else "poly"
        best = None
        best_d = self.POINT_HIT_RADIUS
        for i, p in enumerate(pts):
            vp = self.mapFromScene(p)
            d = (QPointF(vp) - view_pos).manhattanLength()
            if d <= best_d:
                best_d = d
                best = (kind, i)
        return best

    # -- drawing ------------------------------------------------------------

    def _redraw_calibration(self) -> None:
        self._remove_items(self._calib_items)
        pts = self._calib_points
        if len(pts) == 2:
            pen = QPen(_MARK_COLOR)
            pen.setWidth(2)
            pen.setCosmetic(True)
            line = QGraphicsLineItem(pts[0].x(), pts[0].y(), pts[1].x(), pts[1].y())
            line.setPen(pen)
            self._scene.addItem(line)
            self._calib_items.append(line)
        for i, p in enumerate(pts):
            active = self._active == ("scale", i)
            self._add_marker(self._calib_items, p, str(i + 1), _MARK_COLOR,
                             filled=False, active=active)

    def _redraw_polygon(self) -> None:
        self._remove_items(self._poly_items)
        pts = self._poly_points
        color = self._active_color
        edge = QPen(color)
        edge.setWidth(3)          # the active boundary is drawn a touch thicker
        edge.setCosmetic(True)
        for a, b in zip(pts, pts[1:]):
            line = QGraphicsLineItem(a.x(), a.y(), b.x(), b.y())
            line.setPen(edge)
            line.setZValue(5)
            self._scene.addItem(line)
            self._poly_items.append(line)
        if self._poly_closed and len(pts) >= 3:
            closing = QPen(color)
            closing.setWidth(3)
            closing.setCosmetic(True)
            closing.setStyle(Qt.PenStyle.DashLine)
            line = QGraphicsLineItem(pts[-1].x(), pts[-1].y(), pts[0].x(), pts[0].y())
            line.setPen(closing)
            line.setZValue(5)
            self._scene.addItem(line)
            self._poly_items.append(line)
        for i, p in enumerate(pts):
            active = self._active == ("poly", i)
            self._add_marker(self._poly_items, p, str(i + 1), color,
                             filled=True, active=active)

    def _redraw_active(self) -> None:
        """Refresh only the highlight after selection changes (cheap enough to
        just rebuild the relevant layer)."""
        if self._calibrating:
            self._redraw_calibration()
        else:
            self._redraw_polygon()

    def _add_marker(self, bucket: list, scene_pt: QPointF, number: str,
                    color: QColor, *, filled: bool, active: bool) -> None:
        r = 5.0 if filled else 6.0
        pen = QPen(color)
        pen.setWidth(2)
        pen.setCosmetic(True)
        dot = QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
        dot.setPen(pen)
        if filled:
            dot.setBrush(QBrush(color))
        dot.setPos(scene_pt)
        dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        dot.setZValue(10)
        self._scene.addItem(dot)
        bucket.append(dot)

        if active:
            # A white ring marks the point the arrow keys will nudge.
            rr = r + 3
            ring = QGraphicsEllipseItem(-rr, -rr, 2 * rr, 2 * rr)
            hpen = QPen(_ACTIVE_HALO)
            hpen.setWidth(2)
            hpen.setCosmetic(True)
            ring.setPen(hpen)
            ring.setPos(scene_pt)
            ring.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            ring.setZValue(9)
            self._scene.addItem(ring)
            bucket.append(ring)

        label = QGraphicsSimpleTextItem(number)
        label.setBrush(color)
        label.setPos(scene_pt)
        label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        label.moveBy(r + 3, -(r + 13))
        label.setZValue(10)
        self._scene.addItem(label)
        bucket.append(label)

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawForeground(painter, rect)
        if not (self._crosshair_enabled and self.is_picking() and self._cursor_vp_pos is not None):
            return
        x, y = self._cursor_vp_pos.x(), self._cursor_vp_pos.y()
        w, h = self.viewport().width(), self.viewport().height()
        painter.save()
        painter.resetTransform()  # draw in viewport pixels: fixed on-screen size
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # Light halo first (so the line reads on dark map areas), dark core on top.
        halo = QPen(QColor(255, 255, 255, 200))
        halo.setWidth(3)
        painter.setPen(halo)
        painter.drawLine(0, y, w, y)
        painter.drawLine(x, 0, x, h)
        core = QPen(QColor(20, 20, 20))
        core.setWidth(1)
        painter.setPen(core)
        painter.drawLine(0, y, w, y)
        painter.drawLine(x, 0, x, h)
        painter.restore()

    # -- helpers ------------------------------------------------------------

    def _remove_items(self, bucket: list) -> None:
        for item in bucket:
            if item.scene() is self._scene:
                self._scene.removeItem(item)
        bucket.clear()

    def _reset_calibration_state(self) -> None:
        self._remove_items(self._calib_items)
        self._calib_points.clear()
        self._calibrating = False
        if self._active is not None and self._active[0] == "scale":
            self._active = None

    def _reset_calibration_mode(self) -> None:
        """Leave calibration mode and drop its in-progress points (used when
        switching to polygon mode)."""
        self._reset_calibration_state()

    def _reset_polygon_state(self) -> None:
        self._remove_items(self._poly_items)
        self._poly_points.clear()
        self._poly_closed = False
        self._tracing = False
        if self._active is not None and self._active[0] == "poly":
            self._active = None

    def _reset_polygon_mode(self) -> None:
        """Leave tracing mode without discarding the polygon (used when switching
        to scale mode — the boundary should survive)."""
        self._tracing = False

    def _update_cursor(self) -> None:
        if self._pixmap_item is None:
            self.unsetCursor()
        elif self.is_picking():
            # Hide the OS cursor when the drawn crosshair is showing, else a cross.
            self.setCursor(Qt.CursorShape.BlankCursor if self._crosshair_enabled
                           else Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
